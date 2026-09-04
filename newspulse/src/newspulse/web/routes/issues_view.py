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

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import crisis, decision, issues, scenarios, stakeholders
from ... import profile as profiles
from ...models import (
    DECISION_NAME_MAX,
    ISSUE_SCALE_MAX,
    ISSUE_SCALE_MIN,
    STAKEHOLDER_TEXT_MAX,
    Analysis,
    Article,
    Client,
    Crisis,
    DecisionPacket,
    Issue,
    IssueStatus,
    PacketSection,
    ResponseOption,
    Scenario,
    SourceRank,
    StakeholderSelection,
)
from ..app import get_db, templates
from ..filenames import client_slug
from ..mandates import mandate_or_404
from ..redirects import local_target
from . import stakeholder_ui
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


def _selections_by_issue(
    session: Session, rows: list[Issue]
) -> dict[int, list[StakeholderSelection]]:
    """Every issue on the page with its stakeholder selection, in one query.

    Per-issue reads would cost the register two statements per row, and the
    history half of the page is unbounded — the whole record of a mandate that
    has been carried for years. One ``IN`` answers all of them, and the
    ``selectin`` on :attr:`StakeholderSelection.stakeholder` loads the groups
    themselves in one more.
    """
    grouped: dict[int, list[StakeholderSelection]] = {row.id: [] for row in rows}
    if not grouped:
        return grouped
    stored = session.scalars(
        select(StakeholderSelection)
        .where(StakeholderSelection.issue_id.in_(list(grouped)))
        .order_by(StakeholderSelection.issue_id, StakeholderSelection.position)
    ).all()
    for row in stored:
        grouped[row.issue_id].append(row)
    return grouped


# --- Szenarien und Reaktionsoptionen (RIS-04) --------------------------------------
#
# The sentences are constants because every one of them is a key in the i18n
# table: a sentence built with an f-string cannot be looked up, and would render
# German on an English page.

#: Nothing survived the disciplines. Said rather than swallowed: a button that
#: answers with an unchanged page reads as broken, and the reader presses again.
NO_SCENARIOS = (
    "Keine Szenarien gespeichert: ohne prüfbaren Auslöser, mit einer Zahl ohne "
    "Zeile oder als Tatsache formuliert wird ein Verlauf nicht gespeichert."
)
SCENARIOS_FAILED = "Die Szenarien sind fehlgeschlagen. Die Einzelheiten stehen im Log."
#: A set of options that cannot say "nicht reagieren" is a set that can only
#: propose acting, and it is refused rather than shown.
NO_OPTIONS = (
    "Keine Reaktionsoptionen gespeichert: es braucht mindestens drei, darunter "
    "„nicht reagieren“."
)
OPTIONS_FAILED = (
    "Die Reaktionsoptionen sind fehlgeschlagen. Die Einzelheiten stehen im Log."
)
#: The options rest on the courses, so the courses come first.
SCENARIOS_FIRST = (
    "Erst die Szenarien: die Reaktionsoptionen werden gegen sie entwickelt."
)

#: Every sentence this feature can put on a page. The i18n suite walks it, so a
#: note added without its English pair fails there rather than on the evening a
#: reader has the page in English.
SCENARIO_NOTES = (
    NO_SCENARIOS,
    SCENARIOS_FAILED,
    NO_OPTIONS,
    OPTIONS_FAILED,
    SCENARIOS_FIRST,
)


def _scenarios_by_issue(
    session: Session, rows: list[Issue]
) -> dict[int, list[Scenario]]:
    """Every issue on the page with its three courses, in one query.

    Per-issue reads would cost the register two statements per row, and the
    history half of the page is unbounded. The ``selectin`` on
    :attr:`Scenario.triggers` and :attr:`Scenario.groups` loads those in one
    more each, rather than one per scenario.
    """
    grouped: dict[int, list[Scenario]] = {row.id: [] for row in rows}
    if not grouped:
        return grouped
    order = {kind: rank for rank, kind in enumerate(scenarios.ScenarioKind)}
    stored = session.scalars(
        select(Scenario).where(Scenario.issue_id.in_(list(grouped)))
    ).all()
    for row in stored:
        grouped[row.issue_id].append(row)
    for issue_id, courses in grouped.items():
        grouped[issue_id] = sorted(courses, key=lambda row: order[row.kind])
    return grouped


def _options_by_issue(
    session: Session, rows: list[Issue]
) -> dict[int, list[ResponseOption]]:
    """Every issue on the page with its response options, in one query."""
    grouped: dict[int, list[ResponseOption]] = {row.id: [] for row in rows}
    if not grouped:
        return grouped
    stored = session.scalars(
        select(ResponseOption)
        .where(ResponseOption.issue_id.in_(list(grouped)))
        .order_by(ResponseOption.issue_id, ResponseOption.position)
    ).all()
    for row in stored:
        grouped[row.issue_id].append(row)
    return grouped


def _fired_by_issue(courses: dict[int, list[Scenario]]) -> dict[int, list]:
    """The conditions that have already fired, per issue, newest firing first.

    Read off the scenarios already loaded rather than with a query of its own:
    the marks *are* the trigger rows, and a second read would be the same rows
    under a different name. One mark per condition, which is
    :func:`newspulse.scenarios.fired_marks`'s doing and not this page's: a
    condition standing on two courses is one event, and the mark belongs to the
    issue.
    """
    return {
        issue_id: scenarios.fired_marks(rows) for issue_id, rows in courses.items()
    }


def _packets_by_issue(
    session: Session, rows: list[Issue]
) -> dict[int, list[DecisionPacket]]:
    """Every issue on the page with the papers written from it, newest first.

    Keyed on the *register row* rather than on the anchor, because that is where
    a reader looks for them: an escalated issue's papers hang on the crisis it
    became (:func:`_packet_occasion`), and hiding them from the row they were
    written from would make them unreachable from the one page that offers the
    button. One query for both anchors, so an unbounded history costs two reads
    rather than two per row.
    """
    grouped: dict[int, list[DecisionPacket]] = {row.id: [] for row in rows}
    if not grouped:
        return grouped
    by_crisis: dict[int, int] = {
        row.crisis_id: row.id for row in rows if row.crisis_id is not None
    }
    stored = session.scalars(
        select(DecisionPacket)
        .where(
            DecisionPacket.issue_id.in_(list(grouped))
            | DecisionPacket.crisis_id.in_(list(by_crisis) or [0])
        )
        .order_by(DecisionPacket.created_at.desc(), DecisionPacket.id.desc())
    ).all()
    for row in stored:
        anchor = (
            row.issue_id
            if row.issue_id is not None
            else by_crisis.get(row.crisis_id or 0)
        )
        if anchor in grouped:
            grouped[anchor].append(row)
    return grouped


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
    selections = _selections_by_issue(session, open_rows + past)
    courses = _scenarios_by_issue(session, open_rows + past)
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
            # The three courses, the options, and the marks a fired condition
            # left on the row. Past issues too: a closed or escalated issue
            # stays readable with what was thought about it at the time.
            "courses": courses,
            "options": _options_by_issue(session, open_rows + past),
            "fired": _fired_by_issue(courses),
            # The decision papers written from each row (RIS-05). A new one
            # stands beside the old, so this is a list and never one row.
            "packets": _packets_by_issue(session, open_rows + past),
            # The two label maps: the stored values are keys ("bester",
            # "zweites_medium"), and what a reader acts on is a sentence. In
            # Python rather than in Jinja because each label is an i18n key,
            # and a label assembled in a template cannot be looked up.
            "kind_labels": scenarios.KIND_LABELS,
            "condition_labels": scenarios.CONDITION_LABELS,
            # Whether the profile has anything a proposal could rest on: the
            # empty map's sentence says what is missing, with the link there.
            "has_profile": bool(
                profiles.as_prompt_lines(profiles.stored(session, client.id))
            ),
            "stakeholder_note": stakeholder_ui.pop_note(client.id),
            # Whether one of the card's model calls is running for *this*
            # mandate: the buttons spend a call on a worker thread, so without
            # this the page after the redirect looks like a button that did
            # nothing, and the reader presses it again.
            "stakeholder_running": stakeholder_ui.busy(client.id),
            # The stored cap, so the form cannot promise a width the column
            # truncates, and where the map's buttons return to.
            "smap_max": STAKEHOLDER_TEXT_MAX,
            "back_to": f"/client/{client.id}/issues",
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
    """The issue, or ``None`` for a stale id — never a 500 on a button.

    A benchmark's issue is ``None`` as well. :func:`mandate_or_404` keeps the
    workspace *pages* off a company nobody reports to; the buttons have to
    hold the same line, or a hand-typed POST still spends a model call writing
    for a company that will never receive one — which is the harm
    ``web/mandates.py`` was written to end.
    """
    row = session.get(Issue, issue_id)
    if row is None:
        return None
    # ``Issue`` carries no ``client`` relationship, so the mandate is fetched:
    # one keyed read on a button, against a model call spent on a company that
    # will never receive what it writes.
    mandate = session.get(Client, row.client_id)
    if mandate is None or mandate.is_competitor:
        return None
    return row


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
#
# The notes and the order-form reader are ``stakeholder_ui``'s: the same
# sentences answer the same clicks on the crisis page, and one channel means a
# reader looks in one place for the answer to any of the card's buttons.


@router.post("/issues/{issue_id}/stakeholder/auswahl")
def select_stakeholders(
    issue_id: int,
    redirect_to: str = Form("/"),
    session: Session = Depends(get_db),
) -> Response:
    """Build the issue's selection from the standing map, reasons included.

    Idempotent by construction — an issue that already carries a selection
    keeps it, order and all. An empty map selects nothing and says so: the
    selection is *from* the card, so the card comes first. A map that has
    grown since is reached through the Ergänzen button, which only appends.

    The call runs on a worker thread behind ``stakeholder_ui``'s lock: it is a
    three-minute timeout, and a second click would spend a second call.
    """
    standing = _issue_for(session, issue_id)
    if standing is None:
        return RedirectResponse(local_target(redirect_to), status_code=_SEE_OTHER)

    def _select(worker: Session) -> str:
        issue = _issue_for(worker, issue_id)
        if issue is None:
            return ""
        selected = stakeholders.select_for(worker, issue=issue)
        return "" if selected else stakeholder_ui.NO_SELECTION

    stakeholder_ui.spend(
        _select,
        client_id=standing.client_id,
        name=f"newspulse-stakeholder-issue-{issue_id}",
        failed=stakeholder_ui.SELECTION_FAILED,
    )
    return RedirectResponse(local_target(redirect_to), status_code=_SEE_OTHER)


@router.post("/issues/{issue_id}/stakeholder/ergaenzen")
def top_up_stakeholders(
    issue_id: int,
    redirect_to: str = Form("/"),
    session: Session = Depends(get_db),
) -> Response:
    """Ask whether groups added to the map since also belong on this issue.

    The way back into a selection that already stands. Without it the list is
    frozen against the very card it is drawn from: a group put on the map on
    Tuesday could never reach Monday's issue, and the only escape would be
    deleting map rows until the selection empties — which destroys the map.

    It appends and nothing else. The standing rows keep their reasons, their
    positions and their ``position_set_by``, so a person's order survives.
    """
    standing = _issue_for(session, issue_id)
    if standing is None:
        return RedirectResponse(local_target(redirect_to), status_code=_SEE_OTHER)

    def _top_up(worker: Session) -> str:
        issue = _issue_for(worker, issue_id)
        if issue is None:
            return ""
        added = stakeholders.add_to_selection(worker, issue=issue)
        return "" if added else stakeholder_ui.NO_NEW_SELECTED

    stakeholder_ui.spend(
        _top_up,
        client_id=standing.client_id,
        name=f"newspulse-stakeholder-topup-issue-{issue_id}",
        failed=stakeholder_ui.SELECTION_FAILED,
    )
    return RedirectResponse(local_target(redirect_to), status_code=_SEE_OTHER)


@router.post("/issues/{issue_id}/stakeholder/{selection_id}/entfernen")
def drop_stakeholder(
    issue_id: int,
    selection_id: int,
    redirect_to: str = Form("/"),
    session: Session = Depends(get_db),
) -> Response:
    """Take one group off this issue's list, the standing map untouched.

    Here and not on the map, because removing the map row would take the group
    off every occasion at once. No model call: a person removing a group is a
    decision, not a question.
    """
    standing = _issue_for(session, issue_id)
    if standing is not None:
        stakeholders.drop_from_selection(
            session, issue=standing, selection_id=selection_id
        )
    return RedirectResponse(local_target(redirect_to), status_code=_SEE_OTHER)


@router.post("/issues/{issue_id}/stakeholder/reihenfolge")
def reorder_stakeholders(
    request: Request,
    issue_id: int,
    # Both text, not int: a cleared number field posts "" and so does an
    # emptied hidden id, and FastAPI would answer a person pressing a button
    # with raw validation JSON. The coercion — and the sentence when it fails —
    # belongs to :func:`stakeholder_ui.ordered_ids`.
    sid: list[str] = Form(default_factory=list),
    pos: list[str] = Form(default_factory=list),
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
        ordered, refusal = stakeholder_ui.ordered_ids(sid, pos)
        if refusal:
            stakeholder_ui.note(standing.client_id, refusal)
        else:
            try:
                stakeholders.reorder(
                    session, issue=standing, ordered_ids=ordered, by=_who(request)
                )
            except ValueError:
                stakeholder_ui.note(
                    standing.client_id, stakeholder_ui.ORDER_WRONG_ROWS
                )
    return RedirectResponse(local_target(redirect_to), status_code=_SEE_OTHER)


# --- Szenarien und Reaktionsoptionen: the four buttons (RIS-04) --------------------
#
# The lock and the note channel are ``stakeholder_ui``'s. Deliberately, and not
# out of thrift: these buttons sit on the same page as the card's, they spend
# the same three-minute call, and one lock across all of them is what stops a
# reader who presses two of them in a row from paying for both. The answer
# appears where the card's answers appear, which is the one place a reader of
# this page already looks.


@router.post("/issues/{issue_id}/szenarien")
def build_scenarios(
    issue_id: int,
    redirect_to: str = Form("/"),
    session: Session = Depends(get_db),
) -> Response:
    """Develop the issue's three courses, with their checkable triggers.

    Idempotent by construction: an issue that already carries a set keeps it,
    marks and all. Re-asking would replace narratives a consultant has read
    into a meeting and would re-arm triggers that have already fired, which is
    the one thing "einmal gemeldet" forbids — the way to ask again is the
    Verwerfen button beside it, pressed by a person.
    """
    standing = _issue_for(session, issue_id)
    if standing is None:
        return RedirectResponse(local_target(redirect_to), status_code=_SEE_OTHER)

    def _build(worker: Session) -> str:
        issue = _issue_for(worker, issue_id)
        if issue is None:
            return ""
        return "" if scenarios.generate_scenarios(worker, issue) else NO_SCENARIOS

    stakeholder_ui.spend(
        _build,
        client_id=standing.client_id,
        name=f"newspulse-scenarios-issue-{issue_id}",
        failed=SCENARIOS_FAILED,
    )
    return RedirectResponse(local_target(redirect_to), status_code=_SEE_OTHER)


@router.post("/issues/{issue_id}/szenarien/verwerfen")
def drop_scenarios(
    issue_id: int,
    redirect_to: str = Form("/"),
    session: Session = Depends(get_db),
) -> Response:
    """Take the courses off so a person can ask again. No model call.

    The options go with them: they were developed *against* these courses, and
    a list of answers to a question that is no longer on the page is worse than
    no list. Re-arming the triggers is the price, and it is why this is a
    person's button and never a side effect of anything.
    """
    standing = _issue_for(session, issue_id)
    if standing is not None:
        scenarios.clear_options(session, standing)
        scenarios.clear_scenarios(session, standing)
    return RedirectResponse(local_target(redirect_to), status_code=_SEE_OTHER)


@router.post("/issues/{issue_id}/optionen")
def build_options(
    issue_id: int,
    redirect_to: str = Form("/"),
    session: Session = Depends(get_db),
) -> Response:
    """Develop the response options, "nicht reagieren" among them.

    The courses come first: the options are developed against them, and a set
    written without them would be advice about a matter nobody has described a
    course for. Said on the page rather than silently generated anyway.
    """
    standing = _issue_for(session, issue_id)
    if standing is None:
        return RedirectResponse(local_target(redirect_to), status_code=_SEE_OTHER)
    if not scenarios.stored_scenarios(session, standing):
        stakeholder_ui.note(standing.client_id, SCENARIOS_FIRST)
        return RedirectResponse(local_target(redirect_to), status_code=_SEE_OTHER)

    def _build(worker: Session) -> str:
        issue = _issue_for(worker, issue_id)
        if issue is None:
            return ""
        return "" if scenarios.generate_options(worker, issue) else NO_OPTIONS

    stakeholder_ui.spend(
        _build,
        client_id=standing.client_id,
        name=f"newspulse-options-issue-{issue_id}",
        failed=OPTIONS_FAILED,
    )
    return RedirectResponse(local_target(redirect_to), status_code=_SEE_OTHER)


@router.post("/issues/{issue_id}/optionen/verwerfen")
def drop_options(
    issue_id: int,
    redirect_to: str = Form("/"),
    session: Session = Depends(get_db),
) -> Response:
    """Take the options off so a person can ask again. No model call."""
    standing = _issue_for(session, issue_id)
    if standing is not None:
        scenarios.clear_options(session, standing)
    return RedirectResponse(local_target(redirect_to), status_code=_SEE_OTHER)


# --- Das Entscheidungspapier (RIS-05) ----------------------------------------------
#
# The paper is written from the register and read on a page of its own, because
# it is a document rather than a card: it is handed round a room and downloaded,
# and the downloaded copy carries no link back into this application.
#
# The lock and the note channel are ``stakeholder_ui``'s, for the same reason
# RIS-04's four buttons use them: one lock across every model-backed button on
# this page is what stops a reader who presses two of them in a row from paying
# for both, and the answer appears where this page's answers already appear.

#: The answer came back without the one thing a paper cannot do without. Said
#: rather than swallowed: a button that answers with an unchanged page reads as
#: broken, and the reader presses it again.
NO_PACKET = (
    "Kein Entscheidungspapier gespeichert: die Antwort sagte nicht, was passiert "
    "ist."
)
PACKET_FAILED = (
    "Das Entscheidungspapier ist fehlgeschlagen. Die Einzelheiten stehen im Log."
)
#: A decided paper is the record of what a decision rested on. Editing it
#: afterwards is the one thing that would make it worthless, so both forms
#: refuse and say so.
PACKET_DECIDED = (
    "Das Papier ist entschieden und wird nicht mehr geändert. Ein neuer Stand "
    "ist ein neues Papier."
)
DECISION_EMPTY = (
    "Die Entscheidung wurde nicht vermerkt: es fehlt, was entschieden wurde."
)
#: A hand-typed date that is not one. Refused without a write rather than
#: stored as "no deadline", which would read as a deadline nobody set.
DEADLINE_UNREADABLE = (
    "Die Frist wurde nicht gesetzt: das Datum war nicht lesbar."
)

#: Every sentence this feature can put on a page. The i18n suite walks it, so a
#: note added without its English pair fails there rather than on the evening a
#: reader has the page in English.
PACKET_NOTES = (
    NO_PACKET,
    PACKET_FAILED,
    PACKET_DECIDED,
    DECISION_EMPTY,
    DEADLINE_UNREADABLE,
)


def _packet_occasion(session: Session, issue: Issue) -> tuple[Issue | None, Crisis | None]:
    """Which occasion a paper written from this row hangs on.

    An escalated issue's paper hangs on the *crisis*: that is what the matter is
    called from the declaration onwards, and the acceptance asks for a paper
    "zu einem Issue oder einer Krise". The row keeps its ``crisis_id`` from the
    handover, so no second button is needed to reach the second anchor.
    """
    if issue.crisis_id is not None:
        standing = session.get(Crisis, issue.crisis_id)
        if standing is not None:
            return None, standing
    return issue, None


def _packet_or_404(
    session: Session, client_id: int, packet_id: int
) -> tuple[Client, DecisionPacket]:
    """One mandate's paper, or a 404. Never another mandate's.

    The paper carries a company's unconfirmed claims and its contradictions, so
    the mandate guard is the same one every workspace page takes and the id is
    checked against it rather than trusted.
    """
    client = mandate_or_404(session, client_id)
    row = decision.packet(session, client, packet_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Entscheidungspapier nicht gefunden")
    return client, row


def _read_deadline(raw: str) -> tuple[dt.datetime | None, bool]:
    """The date field as a moment, and whether it was readable.

    ``<input type="date">`` posts ``YYYY-MM-DD``; an emptied field posts "",
    which is a deadline being *taken off* and not an error. Anything else was
    submitted around the browser, and the honest answer is the unchanged row —
    a value coerced to "no deadline" would read as a deadline nobody set.

    Local midnight rather than UTC midnight: a Frist is a day in the reader's
    calendar, and storing 00:00 UTC would render as the day before for anybody
    west of Greenwich.
    """
    text = (raw or "").strip()
    if not text:
        return None, True
    try:
        day = dt.date.fromisoformat(text)
    except ValueError:
        return None, False
    return (
        dt.datetime.combine(day, dt.time.min, tzinfo=_local_tz()).astimezone(dt.UTC),
        True,
    )


def _packet_context(
    session: Session,
    client: Client,
    row: DecisionPacket,
    *,
    download: bool,
) -> dict:
    """Everything the paper renders from, identical for screen and export.

    One builder for both, so the downloaded file cannot carry a sentence the
    screen did not — the only thing ``download`` changes is the chrome around
    the content.

    The response options are *named* rather than copied: they are the matter's
    own stored rows with their own provenance, and the same rule the stakeholder
    selection keeps by pointing into the standing map. Everything the model
    wrote is frozen on the packet itself.
    """
    parts = decision.sections(row)
    anchor = decision.anchor_issue(session, row)
    all_gaps = decision.gaps(session, row)
    deadline = row.deadline.astimezone(_local_tz()).date() if row.deadline else None
    return {
        "client": client,
        "packet": row,
        "occasion": decision.occasion(session, row) or client.name,
        "belegt": parts[PacketSection.BELEGT],
        "unbestaetigt": parts[PacketSection.UNBESTAETIGT],
        "offen": parts[PacketSection.OFFEN],
        "options": scenarios.stored_options(session, anchor) if anchor else [],
        "gaps": all_gaps,
        # The decider and the deadline stand at the *top* of the paper when they
        # are missing, which is the acceptance: their absence is the first line
        # and never a blank space.
        "leading_gaps": [gap for gap in all_gaps if gap.leading],
        # The order is printed on the paper, off the enum's own declaration
        # order — the one place the Quellenordnung is written down.
        "source_ranks": list(SourceRank),
        "evidence_labels": decision.EVIDENCE_LABELS,
        "name_max": DECISION_NAME_MAX,
        "deadline_value": f"{deadline:%Y-%m-%d}" if deadline else "",
        "note": stakeholder_ui.pop_note(client.id) if not download else "",
        # The screen carries the nav, the gap links and the two forms; the
        # downloaded file carries none of them, and neither changes a line of
        # content.
        "download": download,
    }


@router.post("/issues/{issue_id}/entscheidungspapier")
def build_packet(
    request: Request,
    issue_id: int,
    redirect_to: str = Form("/"),
    session: Session = Depends(get_db),
) -> Response:
    """Write a decision paper to this matter. A new one stands beside the old.

    Deliberately not idempotent, unlike every other model-backed button on this
    page: "ein neues Papier zum selben Issue ersetzt das alte nicht, sondern
    tritt daneben", because two papers a week apart are the record of how the
    reading changed — which is exactly what gets asked afterwards.
    """
    standing = _issue_for(session, issue_id)
    if standing is None:
        return RedirectResponse(local_target(redirect_to), status_code=_SEE_OTHER)
    who = _who(request)

    def _write(worker: Session) -> str:
        issue = _issue_for(worker, issue_id)
        if issue is None:
            return ""
        mandate = worker.get(Client, issue.client_id)
        anchor, crisis = _packet_occasion(worker, issue)
        written = decision.build(
            worker, mandate, issue=anchor, crisis=crisis, by=who
        )
        return "" if written is not None else NO_PACKET

    stakeholder_ui.spend(
        _write,
        client_id=standing.client_id,
        name=f"newspulse-packet-issue-{issue_id}",
        failed=PACKET_FAILED,
    )
    return RedirectResponse(local_target(redirect_to), status_code=_SEE_OTHER)


@router.get(
    "/client/{client_id}/entscheidungspapier/{packet_id}", response_class=HTMLResponse
)
def packet_page(
    request: Request,
    client_id: int,
    packet_id: int,
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """The paper as it is read in the room, with the two forms under it."""
    client, row = _packet_or_404(session, client_id, packet_id)
    return templates.TemplateResponse(
        request,
        "decision_packet.html",
        _packet_context(session, client, row, download=False),
    )


@router.get("/client/{client_id}/entscheidungspapier/{packet_id}.html")
def packet_export(
    request: Request,
    client_id: int,
    packet_id: int,
    session: Session = Depends(get_db),
) -> Response:
    """The same paper as a file. The same template, so it cannot say more."""
    client, row = _packet_or_404(session, client_id, packet_id)
    stamp = f"{row.created_at.astimezone(_local_tz()):%Y-%m-%d}"
    return templates.TemplateResponse(
        request,
        "decision_packet.html",
        _packet_context(session, client, row, download=True),
        headers={
            "Content-Disposition": (
                'attachment; filename="entscheidungspapier_'
                f'{client_slug(client.name)}_{stamp}.html"'
            )
        },
    )


@router.post("/client/{client_id}/entscheidungspapier/{packet_id}/entscheider")
def set_packet_decider(
    client_id: int,
    packet_id: int,
    decision_maker: str = Form(""),
    deadline: str = Form(""),
    session: Session = Depends(get_db),
) -> Response:
    """A person names who decides and by when. Both are theirs to set.

    Never the tool's: a decider it nominated would be a name nobody agreed to,
    and a deadline it computed would be a promise nobody made. An unreadable
    date changes nothing and says so, rather than quietly storing "no deadline".
    """
    client, row = _packet_or_404(session, client_id, packet_id)
    when, readable = _read_deadline(deadline)
    if not readable:
        stakeholder_ui.note(client.id, DEADLINE_UNREADABLE)
    elif not decision.set_decider(
        session, row, decision_maker=decision_maker, deadline=when
    ):
        stakeholder_ui.note(client.id, PACKET_DECIDED)
    return RedirectResponse(
        f"/client/{client.id}/entscheidungspapier/{row.id}", status_code=_SEE_OTHER
    )


@router.post("/client/{client_id}/entscheidungspapier/{packet_id}/entscheidung")
def record_packet_decision(
    request: Request,
    client_id: int,
    packet_id: int,
    decision_text: str = Form("", alias="decision"),
    session: Session = Depends(get_db),
) -> Response:
    """Note the decision that was taken and who took it; the paper closes.

    A second decision written over the first would erase the answer rather than
    add to it, so it is refused and said: a changed mind is a new paper, which
    is the button on the register.
    """
    client, row = _packet_or_404(session, client_id, packet_id)
    if not decision.record_decision(
        session, row, decision=decision_text, by=_who(request)
    ):
        stakeholder_ui.note(
            client.id, PACKET_DECIDED if row.is_decided else DECISION_EMPTY
        )
    return RedirectResponse(
        f"/client/{client.id}/entscheidungspapier/{row.id}", status_code=_SEE_OTHER
    )
