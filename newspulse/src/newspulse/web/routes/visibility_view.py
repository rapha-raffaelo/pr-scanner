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

One thing DEC-3 asks for is not here yet. "Der Wortlaut jeder Antwort bleibt einen
Klick entfernt" needs a route onto a single stored answer, and the mock for it
(``features/mocks/visibility-answers.html``) is unbuilt; every figure on this page
resolves to a count of answers that *are* stored, and reading one back verbatim is
the story after this one. Nothing here claims otherwise.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import threading
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass, replace
from enum import StrEnum

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import config, visibility
from ...analyzer import AnalyzerError
from ...db import get_session
from ...models import (
    Client,
    VisibilityAnswer,
    VisibilityQuestion,
    VisibilityRun,
)
from .. import spawn
from ..app import get_db, templates
from ..runlock import guard as _run_guard
from .today import _fetch_last_run, _local_tz

router = APIRouter()
_log = logging.getLogger(__name__)

_SEE_OTHER = 303

# One measurement at a time in this process, and the sweep's own guard around it:
# a full set is up to twenty-four questions times every provider times two model
# calls, and a second click while one is running would put the same set to the
# same providers twice.
_measuring = threading.Lock()

#: One proposal run at a time, and what the last one for each mandate produced.
#:
#: In memory rather than in a table, deliberately: ``visibility.propose`` stores
#: nothing, which is the whole of the preview step — a set nobody accepted must
#: not outlive the process as though somebody had. The page collects the answer
#: on its next render and the reader accepts what they want; a restart in between
#: costs one button press, which is the right price for not persisting a set that
#: was never agreed to.
_proposing = threading.Lock()
_proposed: dict[int, list[visibility.Proposal]] = {}
_proposal_error: dict[int, str] = {}

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

#: Where the trend's vertical axis stops while every point still fits under it.
#: The locked mock draws 0–60 %, which is the range a mandate's share actually
#: moves in; against a fixed 0–100 % axis a typical 20–40 % line hugs the floor
#: and a four-point week is invisible on it. A share above this opens the axis to
#: the full range rather than letting the line run off the top of the chart.
_TREND_CEILING = 0.6


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
    """What changed about one cell between two measurements.

    The last two exist because a named answer may carry no position at all — the
    model mentions the company in prose without placing it in a list, which
    ``VisibilityAnswer``'s CHECK (``named OR position IS NULL``) permits. Gaining
    or losing that place is reported as what it is rather than compared as a rank
    against a number that was never measured.
    """

    ENTERED = "entered"
    LEFT = "left"
    ROSE = "rose"
    FELL = "fell"
    RANKED = "ranked"
    UNRANKED = "unranked"


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
class Comparison:
    """What the movement panel may say about two measurements.

    :attr:`comparable` is carried rather than left to be inferred from an empty
    list, because zero of it is its own sentence. Two runs that overlap on no
    cell at all — a provider reached one slice of the set last week and another
    slice this week — have nothing to compare, and rendering that as "nothing
    changed" tells a consultant the week was stable when in truth it was never
    measured against. That is the same outage-reported-as-a-finding this module
    guards against everywhere else, only in the reassuring direction.
    """

    moved: tuple[Movement, ...]
    unchanged: int

    @property
    def comparable(self) -> int:
        """The questions measured in both runs — the ones the panel speaks for."""
        return len(self.moved) + self.unchanged


@dataclass(frozen=True, slots=True)
class Occupant:
    """One company named across the set, and in how many questions."""

    name: str
    questions: int
    share: float
    is_mandate: bool
    #: Its place in the ranking, counted from one over every company named. Carried
    #: because the panel shows only the head of the list and the mandate is never
    #: cut from it: a row shown below the cut has to say which place it holds.
    rank: int


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
    #: The move against the previous measurement, in whole percentage points, or
    #: ``None`` where there is no previous measurement. Whole because a set of at
    #: most two dozen questions moves in chunks of four points and up, so a
    #: decimal here is noise.
    points: int | None
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
    #: The share the top of the chart stands for. Carried because the template
    #: writes the axis labels, and a chart whose plot area and whose axis disagree
    #: is worse than no chart.
    ceiling: float


@dataclass(frozen=True, slots=True)
class Openings:
    """The two findings this measurement supports on its own arithmetic.

    Deliberately only these two. Both are a count over answers this run stored,
    and nothing else; anything richer would be a judgement, and this page reports
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


@dataclass(frozen=True, slots=True)
class _Measurement:
    """One stored run beside the tally read off it.

    Bundled so the tally is counted once per run and per request. The standing,
    the ranking and the trend all read the newest one, and three passes over the
    same answer rows are three places a later filter can be added to only two.
    """

    run: VisibilityRun
    tally: _Tally


@dataclass(frozen=True, slots=True)
class Attempt:
    """The newest attempt, where it is not the standing the page reports.

    Two states, and neither is a figure: a measurement that is still running, and
    one that finished having reached nobody at all. They are the sentences that
    stop a reader from taking the standing below them for this week's.
    """

    running: VisibilityRun | None
    barren: VisibilityRun | None


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

    A run that has not finished is left out for the same reason and a sharper
    one: its answers arrive question by question over minutes, so reading one as
    the standing would put a partial share at the top of the page and then
    explain the shortfall with a sentence about providers that were down. What is
    running is reported as running (:class:`Attempt`), not as a figure.
    """
    return list(
        session.scalars(
            select(VisibilityRun)
            .where(
                VisibilityRun.client_id == client.id,
                VisibilityRun.finished_at.is_not(None),
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
    current: _Measurement,
    previous: _Measurement | None,
    *,
    accepted: int,
) -> Standing:
    """The lead figure, against the measurement before it.

    The share is counted over the questions that were *measured*, not over the
    whole accepted set: a provider that was down would otherwise pull the number
    towards zero and read as "the models stopped naming us". The gap between the
    two counts is stated on the page instead.
    """
    key = client.name.casefold()
    share = _share(current.tally, key)
    delta = None if previous is None else (share - _share(previous.tally, key)) * 100
    return Standing(
        ran_at=current.run.ran_at,
        named=len(current.tally.named_by.get(key, ())),
        measured=len(current.tally.measured),
        accepted=accepted,
        share=share,
        points=_points(delta),
        previous_at=previous.run.ran_at if previous is not None else None,
        providers_failed=tuple(current.run.providers_failed),
        unread=current.run.answers_unread,
    )


def _points(delta: float | None) -> int | None:
    """A delta in whole percentage points, rounded away from zero at the half.

    Rounded here rather than by the template, because Jinja's ``round`` is
    Python's and Python's rounds a half to its even neighbour: a move of exactly
    +0.5 points came out as zero and lost its line entirely — beside a movement
    panel that had just listed the question that produced it.
    """
    if delta is None:
        return None
    return math.floor(delta + 0.5) if delta >= 0 else math.ceil(delta - 0.5)


def _occupants(client: Client, tally: _Tally) -> list[Occupant]:
    """Who occupies this market, ranked, the mandate marked and in the same list.

    In the same list on purpose: the mandate's rank among the companies an
    assistant names *is* the finding, and lifting it out into its own row would
    let a reader miss that four other firms sit above it.

    The mandate gets a row even where no answer named it. "0 von 18" is precisely
    the reading a consultant opens this tab for, and a mandate simply missing from
    the ranking reads as one nobody measured — which is the other fact this page
    exists to tell apart.
    """
    key = client.name.casefold()
    counted = {name: len(questions) for name, questions in tally.named_by.items()}
    counted.setdefault(key, 0)
    spelling = {key: client.name} | tally.spelling
    rows = [
        Occupant(
            name=spelling[name],
            questions=questions,
            share=questions / len(tally.measured) if tally.measured else 0.0,
            is_mandate=name == key,
            rank=0,
        )
        for name, questions in counted.items()
    ]
    rows.sort(key=lambda row: (-row.questions, row.name.casefold()))
    return [replace(row, rank=index + 1) for index, row in enumerate(rows)]


def _ranking(occupants: list[Occupant]) -> tuple[list[Occupant], int, Occupant | None]:
    """The rows the panel shows, how many it left out, and the mandate if it fell out.

    The list is cut at :data:`_OCCUPANTS_MAX` because past a handful the ranking
    is a long tail nobody competes against. The one row that is never cut is the
    mandate's: a mandate sitting below nine rivals is exactly what this page
    exists to state, and folding it into "und N weitere" would hide it in the case
    that matters most. It is shown apart, carrying its rank, rather than promoted
    into a place it does not hold.
    """
    visible = occupants[:_OCCUPANTS_MAX]
    left = occupants[_OCCUPANTS_MAX:]
    apart = next((row for row in left if row.is_mandate), None)
    return visible, len(left) - (1 if apart is not None else 0), apart


def _own_marks(client: Client) -> tuple[str, str]:
    """What makes a stated source the mandate's own: its name, and its host."""
    raw = (client.website or "").strip()
    host = urllib.parse.urlparse(raw if "//" in raw else f"//{raw}").netloc
    return client.name.casefold(), host.removeprefix("www.").casefold().strip("/")


def _is_own(key: str, name: str, host: str) -> bool:
    """Whether a stated source really is the mandate's own page.

    Anchored, never a bare substring. ``mark in key`` is what this was, and it
    badges a stranger's publication as the client's: "test.de" sits inside
    "warentest.de" and a mandate called Test sits inside "Kontest GmbH", and a
    citation the models never made is the one thing this panel may not record.
    Split the way :func:`newspulse.visibility._appears` splits it, because the two
    halves answer different questions. A stated locator is a domain and is judged
    against the mandate's host alone, anchored so a dot to the left is the same
    publisher and a word character is not — "enpal.de" is not found inside
    "enpal-kritik.de". A stated name is judged against the mandate's name on word
    boundaries. Neither is ever asked about the other: a critic's domain carrying
    the client's name is not the client's page.
    """
    if any(mark in key for mark in visibility._LOCATOR_MARKS):
        return bool(host) and visibility._locator_matcher(host).search(key) is not None
    return bool(name) and visibility._named_in(key, name)


def _source_key(raw: str) -> str:
    """One publisher, one key: a stated locator folded to the host it names.

    The path is dropped *before* the tally and not only at display time. A model
    states "https://www.pv-magazine.de/2026/artikel" as readily as it states
    "pv-magazine.de" — routinely both inside one set — and keying the count on the
    whole string put that publisher in the panel twice, under an identical visible
    label, with its citations split between the two rows. Which is exactly the
    quietly-changed count this panel may not produce.

    A stated *name* carries no scheme and no path and comes back untouched, so
    "pv-magazine" and "pv-magazine.de" still stand apart: folding those together
    needs a publisher identity this schema does not have, and inventing one here
    would be the same offence in the other direction.
    """
    key = raw.strip().casefold().removeprefix("https://").removeprefix("http://")
    return key.removeprefix("www.").split("/", 1)[0].strip()


def _source_name(key: str, stated: str) -> str:
    """How the panel writes one source: the publisher, not the path it came on.

    A stated locator is shown as its host — that is the key itself — and a stated
    name is left exactly as it was written, so no publisher gets a capitalisation
    the answer did not give it.
    """
    return stated if stated.casefold() == key else key


def _sources(client: Client, run: VisibilityRun) -> list[Source]:
    """The sources the models stated, with their counts.

    Counted per answer rather than per question: a source cited by both models is
    two citations, and this figure is about what the models lean on, not about
    how many questions it reached.
    """
    name, host = _own_marks(client)
    counts: dict[str, int] = defaultdict(int)
    spelling: dict[str, str] = {}
    for answer in run.answers:
        for raw in answer.sources:
            key = _source_key(raw)
            if not key:
                continue
            spelling.setdefault(key, raw)
            counts[key] += 1
    rows = [
        Source(
            name=_source_name(key, spelling[key]),
            count=count,
            own=_is_own(key, name, host),
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
    """The change between one provider's two answers to one question, if any.

    A cell that names the mandate without placing it — ``named`` true,
    ``position`` null, which the row's own CHECK permits — is never compared as a
    rank. Taking the missing side for a zero read a first placement as a fall.
    Winning or losing the place is its own move, and two answers that both name
    the mandate without one are unchanged.
    """
    if not before.named and after.named:
        return Move(MoveKind.ENTERED, provider, None, after.position)
    if before.named and not after.named:
        return Move(MoveKind.LEFT, provider, before.position, None)
    if not before.named or before.position == after.position:
        return None
    if before.position is None:
        return Move(MoveKind.RANKED, provider, None, after.position)
    if after.position is None:
        return Move(MoveKind.UNRANKED, provider, before.position, None)
    # A rank counts down: position 3 becoming position 2 is a gain.
    kind = MoveKind.ROSE if after.position < before.position else MoveKind.FELL
    return Move(kind, provider, before.position, after.position)


#: Which way each kind of move points. Winning a place in the enumeration counts
#: with being named at all; losing it counts with being dropped.
_UP_KINDS = (MoveKind.ENTERED, MoveKind.ROSE, MoveKind.RANKED)
_DOWN_KINDS = (MoveKind.LEFT, MoveKind.FELL, MoveKind.UNRANKED)


def _direction(moves: tuple[Move, ...]) -> Direction:
    up = sum(1 for move in moves if move.kind in _UP_KINDS)
    down = sum(1 for move in moves if move.kind in _DOWN_KINDS)
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
) -> Comparison:
    """What changed against the previous measurement, and how many did not.

    Only cells measured in *both* runs are compared. A question one run reached
    and the other did not is neither changed nor unchanged — it is a gap — and
    counting it either way would report an outage as a finding. Which is why the
    result is a :class:`Comparison` rather than a list and a count: where the two
    runs overlap on nothing, an empty list of changes means "there was nothing to
    compare", and the page has to say that instead of "nothing changed".
    """
    if run is None or previous is None:
        return Comparison(moved=(), unchanged=0)
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
    return Comparison(moved=tuple(moved), unchanged=unchanged)


# --- The trend -------------------------------------------------------------------


def _mark(at: dt.datetime, index: int, total: int, share: float, ceiling: float) -> Mark:
    """One share, placed in the chart's frame. ``ceiling`` is the top of the axis."""
    span = CHART_RIGHT - CHART_LEFT
    x = CHART_LEFT if total < 2 else CHART_LEFT + index * span / (total - 1)
    reach = min(max(share, 0.0), ceiling) / ceiling
    y = CHART_BOTTOM - reach * (CHART_BOTTOM - CHART_TOP)
    return Mark(x=round(x, 1), y=round(y, 1), at=at)


def _ceiling(shares: list[float]) -> float:
    """The top of the axis: the mock's range while everything fits under it."""
    return _TREND_CEILING if max(shares, default=0.0) <= _TREND_CEILING else 1.0


def _line(
    ordered: list[_Measurement], shares: list[float], ceiling: float
) -> tuple[Mark, ...]:
    """One polyline's marks, or nothing where there is no such line to draw."""
    if not shares:
        return ()
    return tuple(
        _mark(row.run.ran_at, index, len(ordered), share, ceiling)
        for index, (row, share) in enumerate(zip(ordered, shares, strict=True))
    )


def _trend(client: Client, measurements: list[_Measurement]) -> Trend | None:
    """The mandate's share over the measurements, against its strongest rival.

    ``None`` below two measurements: a single point is a dot, and drawing a line
    through it would suggest a direction the tool has no second measurement for.
    The page says so in one line instead.
    """
    if len(measurements) < 2:
        return None
    ordered = list(reversed(measurements))
    key = client.name.casefold()
    rival = _strongest_rival(client, ordered[-1].tally)
    mandate_shares = [_share(row.tally, key) for row in ordered]
    rival_shares = [] if rival is None else [_share(row.tally, rival) for row in ordered]
    ceiling = _ceiling([*mandate_shares, *rival_shares])
    return Trend(
        mandate=_line(ordered, mandate_shares, ceiling),
        rival=_line(ordered, rival_shares, ceiling),
        rival_name="" if rival is None else ordered[-1].tally.spelling[rival],
        ceiling=ceiling,
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


def _attempt(session: Session, client: Client) -> Attempt:
    """What the newest run is doing, where that is not a standing.

    Only one of the two can be true at a time, and both are about the same row:
    the newest run there is. Running, it has no finished stamp and its answers are
    still arriving. Barren, it finished having stored nothing — every provider was
    down — which is the difference between "nobody named us this week" and "nobody
    answered", and the page has to say which.

    "Running" is :func:`newspulse.visibility.is_running` and not merely a missing
    finished stamp, because the same predicate decides whether the mandate may be
    measured again. ``measure`` commits the run row before the first provider is
    asked and the sweep catches and rolls back a failed measurement, so an
    unfinished row is what a crash *normally* leaves behind. Read as running, it
    put "Eine Messung läuft gerade." on the page and took the only manual control
    away — while :func:`newspulse.visibility.due` was answering yes — until some
    later sweep happened to write a finished run.
    """
    latest = visibility.latest_run(session, client)
    if latest is None:
        return Attempt(running=None, barren=None)
    if latest.finished_at is None:
        return Attempt(running=latest if visibility.is_running(latest) else None, barren=None)
    return Attempt(running=None, barren=None if latest.answers else latest)


def _next_due(standing: Standing | None) -> dt.datetime | None:
    """When the window opens again, or ``None`` where the page cannot say.

    Read off the standing, which is the newest run that stored an answer. The
    window itself is counted from the newest run that *spent* the set, and that
    can be a later one whose answers all came back unreadable — so a date that has
    already passed is not a date, it is that case, and stating it would be worse
    than saying nothing.
    """
    if standing is None or config.VISIBILITY_EVERY_DAYS <= 0:
        return None
    opens = standing.ran_at + dt.timedelta(days=config.VISIBILITY_EVERY_DAYS)
    return opens if opens > dt.datetime.now(dt.UTC) else None


def _context(
    session: Session,
    client: Client,
    questions: list[VisibilityQuestion],
    measurements: list[_Measurement],
) -> dict[str, object]:
    """Everything the page states about the measurements themselves."""
    current = measurements[0] if measurements else None
    previous = measurements[1] if len(measurements) > 1 else None
    standing = (
        None
        if current is None
        else _standing(client, current, previous, accepted=len(questions))
    )
    occupants, left_over, apart = _ranking(
        [] if current is None else _occupants(client, current.tally)
    )
    comparison = _movement(
        None if current is None else current.run,
        None if previous is None else previous.run,
        questions,
    )
    attempt = _attempt(session, client)
    return {
        "questions": questions,
        "question_rows": _question_rows(None if current is None else current.run, questions),
        "standing": standing,
        "occupants": occupants,
        "occupants_left": left_over,
        "mandate_row": apart,
        "sources": [] if current is None else _sources(client, current.run),
        "openings": None if current is None else _openings(current.run),
        "movement": comparison.moved,
        "unchanged": comparison.unchanged,
        "comparable": comparison.comparable,
        "trend": _trend(client, measurements),
        "running": attempt.running,
        "barren": attempt.barren,
        # The button only where pressing it would spend a measurement; where it
        # would not, the page says when the window opens instead of offering a
        # control that quietly does nothing. ``_measuring`` is consulted for the
        # same reason: between the click and the run row this process commits
        # there is a gap, and inside it the lock is the only thing that knows a
        # measurement was already asked for.
        "can_measure": (
            bool(questions)
            and attempt.running is None
            and not _measuring.locked()
            and visibility.due(session, client)
        ),
        "next_due": _next_due(standing),
    }


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
    measurements = [
        _Measurement(run, _tally(run, client))
        for run in _history(session, client, limit=_TREND_RUNS)
    ]
    return templates.TemplateResponse(
        request,
        "client_visibility.html",
        _context(session, client, questions, measurements)
        | {
            "client": client,
            # Whatever the worker left, unless a caller passed something itself.
            "proposals": (
                proposals if proposals is not None else _proposed.get(client.id)
            ),
            "visibility_error": error or _proposal_error.get(client.id),
            # Only for *this* mandate: the lock is process-wide, so asking it
            # alone would put a spinner on every other mandate's page too.
            "proposing": _proposing.locked() and client.id not in _proposed,
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


def _run_proposal(client_id: int) -> None:
    """Ask the panel on a worker thread; always give the lock back.

    Its own session, because the request's is closed by the time this runs.
    """
    try:
        with get_session() as session:
            client = session.get(Client, client_id)
            if client is None or client.is_competitor:
                return
            _proposal_error.pop(client_id, None)
            _proposed[client_id] = visibility.propose(session, client)
    except AnalyzerError as exc:
        _proposal_error[client_id] = str(exc)
    except Exception as exc:  # noqa: BLE001 — a worker thread must never die silently
        _proposal_error[client_id] = f"Der Vorschlag ist fehlgeschlagen: {exc}"
        _log.exception("the visibility proposal failed")
    finally:
        _proposing.release()


@router.post("/client/{client_id}/ki/vorschlag")
def propose_questions(client_id: int, session: Session = Depends(get_db)) -> Response:
    """Ask for a question set on a thread, never inside this response.

    Measured against the live database: twenty-nine seconds for one mandate. Done
    inline that is half a minute of a browser sitting on a submitted form with
    nothing on screen to say why — reported, correctly, as "this does not work" —
    and a proxy timing out in the middle would throw away a model call that had
    already been paid for. So the click starts the run and redirects, and the page
    says a proposal is under way until it can show one, the same shape the
    measurement beside it has always had.

    Stores nothing, still. The answer waits in ``_proposed`` for the page to
    collect it; only accepting a question writes a row.
    """
    client = _client_or_404(session, client_id)
    # Last time's answer goes before this one is asked, or the page would show a
    # stale set beside a notice saying a new one is being written.
    _proposed.pop(client.id, None)
    _proposal_error.pop(client.id, None)
    if _proposing.acquire(blocking=False):
        spawn.start_or_release(
            _run_proposal,
            args=(client.id,),
            name=f"newspulse-vis-proposal-{client.id}",
            release=_proposing.release,
        )
    return RedirectResponse(f"/client/{client_id}/ki", status_code=_SEE_OTHER)


def _measure_one(client_id: int) -> None:
    """Put one mandate's set to the providers. Both guards are already held.

    :func:`newspulse.visibility.measure` is the same call the sweep makes and
    obeys the same window: pressed inside it, it hands back the stored run and
    spends nothing.
    """
    with get_session() as session:
        client = session.get(Client, client_id)
        if client is None or client.is_competitor:
            return
        name = client.name
        run = visibility.measure(session, client)
        if run is None:
            _log.info("nothing to measure for %r", name)
            return
        _log.info(
            "visibility measured for %r on request: %d answer(s)", name, len(run.answers)
        )


def _run_measurement(client_id: int) -> None:
    """Measure one mandate on a worker thread; always give the lock back.

    Behind :data:`newspulse.web.runlock.guard`, the same lock a dashboard-started
    sweep takes, so a click and the 06:10 run cannot put the same set to the same
    providers at once — and the header's spinner says the machine is busy while it
    does. Taken without blocking, never waited on: waiting held :data:`_measuring`
    for the whole length of a sweep, and ``can_measure`` on the page cannot see
    that lock, so the button kept being offered while every further click was a
    silent no-op. A mandate the sweep is standing in the way of is one the sweep
    itself measures — it is due, and the click costs nothing to drop.
    """
    try:
        if not _run_guard.acquire(blocking=False):
            _log.info(
                "a sweep holds the run guard; leaving the visibility measurement "
                "for client %d to it",
                client_id,
            )
            return
        try:
            _measure_one(client_id)
        finally:
            _run_guard.release()
    except Exception:  # noqa: BLE001 — a worker thread must never die silently
        _log.exception("the requested visibility measurement failed")
    finally:
        _measuring.release()


@router.post("/client/{client_id}/ki/messen")
def measure_now(client_id: int, session: Session = Depends(get_db)) -> Response:
    """Put the set to the providers now — on a thread, never inside this response.

    A full set is up to :data:`newspulse.visibility.MAX_QUESTIONS` questions times
    every provider times two model calls, each bounded only by ``ANALYZER_TIMEOUT``:
    measuring inside the request would hold a worker for as long as that takes and
    hand the reader a timed-out page for work that did in fact happen. So the
    click starts the same call the sweep makes and redirects; the page then reports
    the run as running, because the row is committed before the first provider is
    asked.
    """
    client = _client_or_404(session, client_id)
    if visibility.due(session, client) and _measuring.acquire(blocking=False):
        spawn.start_or_release(
            _run_measurement,
            args=(client_id,),
            name=f"newspulse-visibility-{client_id}",
            release=_measuring.release,
        )
    return RedirectResponse(f"/client/{client_id}/ki", status_code=_SEE_OTHER)


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
    "Attempt",
    "Cell",
    "CellState",
    "Comparison",
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
