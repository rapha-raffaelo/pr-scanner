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

from ... import crisis, issues, scenarios, stakeholders
from ... import profile as profiles
from ...models import (
    ISSUE_SCALE_MAX,
    ISSUE_SCALE_MIN,
    STAKEHOLDER_TEXT_MAX,
    Analysis,
    Article,
    Client,
    Issue,
    IssueStatus,
    ResponseOption,
    Scenario,
    StakeholderSelection,
)
from ..app import get_db, templates
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
    """The triggers that have already fired, per issue, newest firing first.

    Read off the scenarios already loaded rather than with a query of its own:
    the marks *are* the trigger rows, and a second read would be the same rows
    under a different name.
    """
    fired: dict[int, list] = {}
    for issue_id, rows in courses.items():
        marks = [
            trigger
            for scenario in rows
            for trigger in scenario.triggers
            if trigger.has_fired
        ]
        fired[issue_id] = sorted(marks, key=lambda row: row.fired_at, reverse=True)
    return fired


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
