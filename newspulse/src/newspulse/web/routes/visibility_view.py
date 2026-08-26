"""KI-Sichtbarkeit: where the mandate stands when an assistant is asked, and
what moved since the last measurement.

The page follows DEC-3 C. It leads with the figure a consultant repeats in a
client meeting — the share of the question set that named the mandate — and then
earns it: who else occupies the market, which sources the models leaned on, and
what changed against the previous measurement.

Three properties of this view are load-bearing, and each of them is a number the
agency would otherwise report wrongly.

* **A failed provider is not a negative answer.** A cell with no row is rendered
  as "nicht gemessen" naming the provider, never as "nicht genannt". The same
  distinction governs the arithmetic: the share is counted over the questions
  that were actually measured, so a provider that was down lowers the confidence
  in the figure rather than the figure itself.
* **Movement is only movement where both measurements exist.** A cell compared
  against a cell that was never measured would report an outage as a loss, which
  is the one direction a client acts on. So only cells present in *both* runs are
  compared, and questions whose result is unchanged are counted rather than
  listed — that counting is the whole reason a weekly measurement exists.
* **Nothing is stored without a click.** ``propose`` spends one model call and
  writes nothing; the proposals it returns are rendered with an accept control
  each, and :func:`newspulse.visibility.accept` is the only thing on this page
  that creates a question.

Benchmarks (``is_competitor``) get no page here at all. They appear inside a
mandate's ranking as named companies, which is the same exclusion the sidebar and
the portfolio already apply.
"""

from __future__ import annotations

import datetime as dt
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import config, visibility
from ...analyzer import AnalyzerError
from ...models import (
    Client,
    VisibilityAnswer,
    VisibilityQuestion,
    VisibilityRun,
)
from ..app import get_db, templates
from .today import _fetch_last_run, _local_tz

router = APIRouter()

_SEE_OTHER = 303

#: How many measurements the trend line carries. Two months of a weekly
#: measurement: far enough back that a seasonal swing is visible, short enough
#: that the line stays readable at the width of half a page.
_TREND_RUNS = 8

#: How many companies the market panel lists. The ranking answers "who occupies
#: this market", and past a handful the answer is a long tail nobody competes
#: with; the count of what is left over is stated rather than dropped.
_OCCUPANTS_MAX = 8

# The trend chart's frame, in the coordinates of its viewBox. Named because the
# template draws grid lines and axis labels against exactly these numbers, and a
# chart whose plot area and whose axis disagree is worse than no chart.
CHART_WIDTH = 480
CHART_LEFT = 80.0
CHART_RIGHT = 416.0
CHART_TOP = 14.0
CHART_BOTTOM = 80.0


class CellState(StrEnum):
    """What one provider's result for one question is, in the three states.

    The middle and the last are the distinction the whole feature turns on:
    :attr:`UNNAMED` is an answer that did not name the mandate, :attr:`UNMEASURED`
    is no answer at all. Rendering the second as the first would put a number in
    a client report that the measurement never supported.
    """

    NAMED = "named"
    UNNAMED = "unnamed"
    UNMEASURED = "unmeasured"


class MoveKind(StrEnum):
    """What changed about one cell between two measurements."""

    ENTERED = "entered"
    LEFT = "left"
    ROSE = "rose"
    FELL = "fell"


class Direction(StrEnum):
    """Which way one question moved, over all its providers together."""

    UP = "up"
    DOWN = "down"
    #: Mixed, not unchanged: a question that gained on one provider and lost on
    #: the other. An unchanged question is counted, never listed.
    MIXED = "mixed"


@dataclass(frozen=True, slots=True)
class Cell:
    """One provider's result for one question in one run."""

    provider: str
    state: CellState
    position: int | None


@dataclass(frozen=True, slots=True)
class QuestionRow:
    """One accepted question and what every asked provider answered."""

    id: int
    text: str
    band: str
    cells: tuple[Cell, ...]


@dataclass(frozen=True, slots=True)
class Move:
    """One provider's change on one question, in facts rather than in a sentence.

    Deliberately not a composed string: the sentence belongs in the template so
    it exists in German and English through ``i18n.py`` like every other visible
    string, rather than being written once here in one language.
    """

    kind: MoveKind
    provider: str
    before: int | None
    after: int | None


@dataclass(frozen=True, slots=True)
class Movement:
    """One question whose result changed, and every provider it changed on."""

    text: str
    direction: Direction
    moves: tuple[Move, ...]


@dataclass(frozen=True, slots=True)
class Occupant:
    """One company named across the set, and in how many questions."""

    name: str
    questions: int
    share: float
    is_mandate: bool


@dataclass(frozen=True, slots=True)
class Source:
    """One source the models stated, and how often."""

    name: str
    count: int
    own: bool


@dataclass(frozen=True, slots=True)
class Standing:
    """The figure the page leads with, and everything that qualifies it."""

    ran_at: dt.datetime
    named: int
    measured: int
    accepted: int
    share: float
    delta: float | None
    previous_at: dt.datetime | None
    providers_failed: tuple[str, ...]
    unread: int


@dataclass(frozen=True, slots=True)
class Mark:
    """One measurement's point on the trend line."""

    x: float
    y: float
    at: dt.datetime


@dataclass(frozen=True, slots=True)
class Trend:
    """The mandate's share over the last measurements, against its strongest rival."""

    mandate: tuple[Mark, ...]
    rival: tuple[Mark, ...]
    rival_name: str


@dataclass(frozen=True, slots=True)
class Openings:
    """The two findings this measurement supports on its own arithmetic.

    Deliberately only these two. Both resolve to a count of stored answers a
    reader can open; anything richer would be a judgement, and this page reports
    rather than advises.
    """

    nobody: int
    rivals_only: int


@dataclass(slots=True)
class _Tally:
    """Which questions each company was named in, for one run."""

    measured: set[int]
    named_by: dict[str, set[int]]
    #: The first spelling each company was seen under, keyed by its folded name.
    spelling: dict[str, str]


# --- Reading one run -------------------------------------------------------------


def _client_or_404(session: Session, client_id: int) -> Client:
    """The mandate, or 404 — and a benchmark is a 404 here.

    ``is_competitor`` companies are yardsticks, not mandates: the sidebar leaves
    them out and the portfolio leaves them out, and a page of their own would
    invite somebody to accept a question set for one and spend a weekly
    measurement on a company nobody reports to.
    """
    client = session.get(Client, client_id)
    if client is None or client.is_competitor:
        raise HTTPException(status_code=404, detail="Client not found")
    return client


def _history(session: Session, client: Client, *, limit: int) -> list[VisibilityRun]:
    """The measurements that actually spent the set, newest first.

    A run with no answer row is not a measurement of anything — every provider
    was down — so it is not what a standing is read off and not a point on a
    trend. It is still reported, separately, as the failed attempt it was.
    """
    return list(
        session.scalars(
            select(VisibilityRun)
            .where(
                VisibilityRun.client_id == client.id,
                VisibilityRun.answers.any(),
            )
            .order_by(VisibilityRun.ran_at.desc(), VisibilityRun.id.desc())
            .limit(limit)
        ).all()
    )


def _cells(run: VisibilityRun) -> dict[tuple[int, str], VisibilityAnswer]:
    """Every stored answer of one run, keyed by (question, provider)."""
    return {(row.question_id, row.provider): row for row in run.answers}


def _tally(run: VisibilityRun, client: Client) -> _Tally:
    """Which questions named which company in this run.

    Counted per *question* rather than per answer: two providers naming the same
    company for the same question is one question that names it, and counting it
    twice would make the ranking depend on how many providers happened to answer.

    The mandate's own membership is read off ``named`` rather than off the company
    list, because that flag is the one this tool decided itself, against the
    mandate's stored aliases, instead of taking the reading model's word for it.
    """
    tally = _Tally(measured=set(), named_by=defaultdict(set), spelling={})
    for answer in run.answers:
        tally.measured.add(answer.question_id)
        if answer.named:
            _count(tally, client.name, answer.question_id)
        for name in answer.companies:
            if name.casefold() != client.name.casefold():
                _count(tally, name, answer.question_id)
    return tally


def _count(tally: _Tally, name: str, question_id: int) -> None:
    """Record one company as named in one question, under its first spelling."""
    key = name.casefold()
    tally.spelling.setdefault(key, name)
    tally.named_by[key].add(question_id)


def _share(tally: _Tally, key: str) -> float:
    """A company's share of the questions this run measured. Zero on an empty run."""
    if not tally.measured:
        return 0.0
    return len(tally.named_by.get(key, ())) / len(tally.measured)


def _standing(
    client: Client,
    run: VisibilityRun,
    previous: VisibilityRun | None,
    *,
    accepted: int,
) -> Standing:
    """The lead figure, against the measurement before it.

    The share is counted over the questions that were *measured*, not over the
    whole accepted set: a provider that was down would otherwise pull the number
    towards zero and read as "the models stopped naming us". The gap between the
    two counts is stated on the page instead.
    """
    tally = _tally(run, client)
    key = client.name.casefold()
    share = _share(tally, key)
    delta = None
    if previous is not None:
        delta = (share - _share(_tally(previous, client), key)) * 100
    return Standing(
        ran_at=run.ran_at,
        named=len(tally.named_by.get(key, ())),
        measured=len(tally.measured),
        accepted=accepted,
        share=share,
        delta=delta,
        previous_at=previous.ran_at if previous is not None else None,
        providers_failed=tuple(run.providers_failed),
        unread=run.answers_unread,
    )


def _occupants(client: Client, tally: _Tally) -> list[Occupant]:
    """Who occupies this market, the mandate marked and in the same list.

    In the same list on purpose: the mandate's rank among the companies an
    assistant names *is* the finding, and lifting it out into its own row would
    let a reader miss that four other firms sit above it.
    """
    key = client.name.casefold()
    rows = [
        Occupant(
            name=tally.spelling[name],
            questions=len(questions),
            share=len(questions) / len(tally.measured) if tally.measured else 0.0,
            is_mandate=name == key,
        )
        for name, questions in tally.named_by.items()
    ]
    rows.sort(key=lambda row: (-row.questions, row.name.casefold()))
    return rows


def _own_marks(client: Client) -> tuple[str, ...]:
    """What makes a stated source the mandate's own: its name, and its host."""
    marks = [client.name.casefold()]
    raw = (client.website or "").strip()
    host = urllib.parse.urlparse(raw if "//" in raw else f"//{raw}").netloc
    host = host.removeprefix("www.").casefold().strip("/")
    if host:
        marks.append(host)
    return tuple(mark for mark in marks if mark)


def _sources(client: Client, run: VisibilityRun) -> list[Source]:
    """The sources the models stated, with their counts.

    Counted per answer rather than per question: a source cited by both models is
    two citations, and this figure is about what the models lean on, not about
    how many questions it reached.
    """
    marks = _own_marks(client)
    counts: dict[str, int] = defaultdict(int)
    spelling: dict[str, str] = {}
    for answer in run.answers:
        for raw in answer.sources:
            key = raw.casefold().removeprefix("https://").removeprefix("http://")
            key = key.removeprefix("www.").rstrip("/")
            if not key:
                continue
            spelling.setdefault(key, raw)
            counts[key] += 1
    rows = [
        Source(
            name=spelling[key],
            count=count,
            own=any(mark in key for mark in marks),
        )
        for key, count in counts.items()
    ]
    rows.sort(key=lambda row: (-row.count, row.name.casefold()))
    return rows


def _openings(run: VisibilityRun) -> Openings:
    """Questions no company was named in, and questions the mandate was left out of.

    Counted over the answers of one question together: a question one model
    answered with nobody and the other answered with four firms is not an empty
    field, and reporting it as one would send a consultant at a question that is
    already occupied.
    """
    named_any: dict[int, bool] = defaultdict(bool)
    named_us: dict[int, bool] = defaultdict(bool)
    for answer in run.answers:
        named_any[answer.question_id] |= bool(answer.companies)
        named_us[answer.question_id] |= answer.named
    return Openings(
        nobody=sum(1 for qid in named_any if not named_any[qid]),
        rivals_only=sum(1 for qid in named_any if named_any[qid] and not named_us[qid]),
    )


def _state(answer: VisibilityAnswer | None) -> CellState:
    if answer is None:
        return CellState.UNMEASURED
    return CellState.NAMED if answer.named else CellState.UNNAMED


def _question_rows(
    run: VisibilityRun | None, questions: list[VisibilityQuestion]
) -> list[QuestionRow]:
    """Every accepted question with one cell per provider the run asked.

    A provider the run asked and has no row for is ``nicht gemessen``, whether it
    errored outright or the measurement stopped before reaching this question.
    Both are the same fact for a reader: nobody answered, so there is nothing to
    read as a negative.

    A mandate that has never been measured still gets its whole set here, with no
    cell against any of it: the set is the thing a consultant reviews and retires
    a question from, and hiding it until the first sweep would make the page look
    empty for a week after somebody filled it.
    """
    stored = _cells(run) if run is not None else {}
    providers = run.providers_asked if run is not None else []
    rows = []
    for question in questions:
        cells = []
        for provider in providers:
            answer = stored.get((question.id, provider))
            cells.append(
                Cell(
                    provider=provider,
                    state=_state(answer),
                    position=answer.position if answer is not None else None,
                )
            )
        rows.append(
            QuestionRow(
                id=question.id,
                text=question.text,
                band=str(question.band),
                cells=tuple(cells),
            )
        )
    return rows


# --- What moved ------------------------------------------------------------------


def _move(provider: str, before: VisibilityAnswer, after: VisibilityAnswer) -> Move | None:
    """The change between one provider's two answers to one question, if any."""
    if not before.named and after.named:
        return Move(MoveKind.ENTERED, provider, None, after.position)
    if before.named and not after.named:
        return Move(MoveKind.LEFT, provider, before.position, None)
    if not before.named or before.position == after.position:
        return None
    # A rank counts down: position 3 becoming position 2 is a gain.
    kind = (
        MoveKind.ROSE
        if (after.position or 0) < (before.position or 0)
        else MoveKind.FELL
    )
    return Move(kind, provider, before.position, after.position)


def _direction(moves: tuple[Move, ...]) -> Direction:
    up = sum(1 for move in moves if move.kind in (MoveKind.ENTERED, MoveKind.ROSE))
    down = sum(1 for move in moves if move.kind in (MoveKind.LEFT, MoveKind.FELL))
    if up and not down:
        return Direction.UP
    if down and not up:
        return Direction.DOWN
    return Direction.MIXED


#: The order the movement panel reads in: what was won, what was lost, what did
#: both. A mixed question last because it needs the two above it for context.
_DIRECTION_ORDER = {Direction.UP: 0, Direction.DOWN: 1, Direction.MIXED: 2}


def _movement(
    run: VisibilityRun | None,
    previous: VisibilityRun | None,
    questions: list[VisibilityQuestion],
) -> tuple[list[Movement], int]:
    """What changed against the previous measurement, and how many did not.

    Only cells measured in *both* runs are compared. A question one run reached
    and the other did not is neither changed nor unchanged — it is a gap — and
    counting it either way would report an outage as a finding.
    """
    if run is None or previous is None:
        return [], 0
    now, then = _cells(run), _cells(previous)
    moved: list[Movement] = []
    unchanged = 0
    for question in questions:
        pairs = [
            (provider, then[(question.id, provider)], now[(question.id, provider)])
            for provider in run.providers_asked
            if (question.id, provider) in now and (question.id, provider) in then
        ]
        if not pairs:
            continue
        moves = tuple(
            move
            for provider, before, after in pairs
            if (move := _move(provider, before, after)) is not None
        )
        if not moves:
            unchanged += 1
            continue
        moved.append(Movement(question.text, _direction(moves), moves))
    moved.sort(key=lambda row: (_DIRECTION_ORDER[row.direction], row.text.casefold()))
    return moved, unchanged


# --- The trend -------------------------------------------------------------------


def _mark(at: dt.datetime, index: int, total: int, share: float) -> Mark:
    """One share, placed in the chart's frame. Shares run 0–1 over the full height."""
    span = CHART_RIGHT - CHART_LEFT
    x = CHART_LEFT if total < 2 else CHART_LEFT + index * span / (total - 1)
    y = CHART_BOTTOM - min(max(share, 0.0), 1.0) * (CHART_BOTTOM - CHART_TOP)
    return Mark(x=round(x, 1), y=round(y, 1), at=at)


def _trend(client: Client, history: list[VisibilityRun]) -> Trend | None:
    """The mandate's share over the measurements, against its strongest rival.

    ``None`` below two measurements: a single point is a dot, and drawing a line
    through it would suggest a direction the tool has no second measurement for.
    The page says so in one line instead.
    """
    if len(history) < 2:
        return None
    runs = list(reversed(history))
    tallies = [_tally(run, client) for run in runs]
    key = client.name.casefold()
    total = len(runs)
    mandate = tuple(
        _mark(run.ran_at, index, total, _share(tally, key))
        for index, (run, tally) in enumerate(zip(runs, tallies, strict=True))
    )
    rival = _strongest_rival(client, tallies[-1])
    if rival is None:
        return Trend(mandate=mandate, rival=(), rival_name="")
    return Trend(
        mandate=mandate,
        rival=tuple(
            _mark(run.ran_at, index, total, _share(tally, rival))
            for index, (run, tally) in enumerate(zip(runs, tallies, strict=True))
        ),
        rival_name=tallies[-1].spelling[rival],
    )


def _strongest_rival(client: Client, tally: _Tally) -> str | None:
    """The folded name of the company named in most questions, mandate aside."""
    key = client.name.casefold()
    others = sorted(
        ((name, len(questions)) for name, questions in tally.named_by.items() if name != key),
        key=lambda pair: (-pair[1], pair[0]),
    )
    return others[0][0] if others else None


# --- The page --------------------------------------------------------------------


def _render(
    request: Request,
    session: Session,
    client: Client,
    *,
    proposals: list[visibility.Proposal] | None = None,
    error: str | None = None,
) -> HTMLResponse:
    """Assemble everything the page states from the stored measurements."""
    questions = visibility.accepted(session, client)
    history = _history(session, client, limit=_TREND_RUNS)
    run = history[0] if history else None
    previous = history[1] if len(history) > 1 else None
    tally = _tally(run, client) if run is not None else None
    occupants = _occupants(client, tally) if tally is not None else []
    moved, unchanged = _movement(run, previous, questions)
    latest = visibility.latest_run(session, client)
    # A run that spent two providers and stored nothing is the failed attempt it
    # was, and it is newer than the standing the page is built from. Saying so is
    # the difference between "nobody named us this week" and "nobody answered".
    barren = (
        latest
        if latest is not None and latest.finished_at is not None and not latest.answers
        else None
    )
    return templates.TemplateResponse(
        request,
        "client_visibility.html",
        {
            "client": client,
            "questions": questions,
            "question_rows": _question_rows(run, questions),
            "standing": _standing(client, run, previous, accepted=len(questions))
            if run is not None
            else None,
            "occupants": occupants[:_OCCUPANTS_MAX],
            "occupants_left": max(len(occupants) - _OCCUPANTS_MAX, 0),
            "sources": _sources(client, run) if run is not None else [],
            "openings": _openings(run) if run is not None else None,
            "movement": moved,
            "unchanged": unchanged,
            "trend": _trend(client, history),
            "measurements": len(history),
            "barren": barren,
            "proposals": proposals,
            "visibility_error": error,
            "every_days": config.VISIBILITY_EVERY_DAYS,
            "max_questions": visibility.MAX_QUESTIONS,
            "chart_width": CHART_WIDTH,
            "chart_left": CHART_LEFT,
            "chart_right": CHART_RIGHT,
            "chart_top": CHART_TOP,
            "chart_bottom": CHART_BOTTOM,
            "last_run": _fetch_last_run(session),
            "header_date": dt.datetime.now(_local_tz()).date(),
        },
    )


@router.get("/client/{client_id}/ki", response_class=HTMLResponse)
def visibility_page(
    request: Request, client_id: int, session: Session = Depends(get_db)
) -> HTMLResponse:
    """Where the mandate stands, and what moved since the previous measurement."""
    return _render(request, session, _client_or_404(session, client_id))


@router.post("/client/{client_id}/ki/vorschlag", response_class=HTMLResponse)
def propose_questions(
    request: Request, client_id: int, session: Session = Depends(get_db)
) -> HTMLResponse:
    """Ask for a question set and render it. Stores nothing.

    Behind a button rather than on the page load, for two reasons that point the
    same way: it costs a model call, and a proposal that regenerates on every
    refresh is a set nobody can read twice. The answer is rendered straight into
    this response because there is nothing to redirect to — no row was created.
    """
    client = _client_or_404(session, client_id)
    try:
        proposals = visibility.propose(session, client)
    except (AnalyzerError, visibility.ParseError) as exc:
        session.rollback()
        return _render(request, session, client, error=str(exc))
    return _render(request, session, client, proposals=proposals)


@router.post("/client/{client_id}/ki/fragen")
def accept_question(
    request: Request,
    client_id: int,
    text: str = Form(...),
    band: str = Form(...),
    session: Session = Depends(get_db),
) -> Response:
    """Put one proposed question into the set. The only thing here that stores one."""
    client = _client_or_404(session, client_id)
    try:
        visibility.accept(session, client, text, band)
    except (visibility.SetFull, ValueError) as exc:
        session.rollback()
        return _render(request, session, client, error=str(exc))
    return RedirectResponse(f"/client/{client_id}/ki", status_code=_SEE_OTHER)


@router.post("/client/{client_id}/ki/fragen/{question_id}/verwerfen")
def reject_question(
    client_id: int, question_id: int, session: Session = Depends(get_db)
) -> Response:
    """Take one question out of the set. Retired, never deleted.

    The answers it produced stay attached to it, because the movement panel
    compares this week against a week whose questions still have to resolve.
    """
    client = _client_or_404(session, client_id)
    question = session.get(VisibilityQuestion, question_id)
    if question is None or question.client_id != client.id:
        raise HTTPException(status_code=404, detail="Question not found")
    visibility.retire(session, question)
    return RedirectResponse(f"/client/{client_id}/ki", status_code=_SEE_OTHER)


__all__ = [
    "Cell",
    "CellState",
    "Direction",
    "Move",
    "MoveKind",
    "Movement",
    "Occupant",
    "Openings",
    "QuestionRow",
    "Source",
    "Standing",
    "Trend",
    "router",
]
