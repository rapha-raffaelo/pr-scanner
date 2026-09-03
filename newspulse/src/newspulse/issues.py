"""The issue register: the thing that gets three weeks old, as rows (RIS-02).

Until now the same accusation on Monday and on Friday was two cards on two days.
This module is the object between the daily card and the declared crisis: one
repeated matter with an age, a last movement and a growing count of attached
signals. The value is in the attaching, not the opening — two signals on one
row are what "something is growing" is made of.

Four disciplines govern it, and each has one owner here:

* **The tool proposes, a person opens** (DEC-3 option A). :func:`propose` reads
  the stored coverage and offers an issue when a matter repeats — two stories on
  two different days, or a story plus a dated market signal of the same matter —
  and it writes nothing at all. :func:`accept` is the person's click;
  :func:`dismiss` is the one-click false alarm, and the same repetition then
  stops being offered.
* **A signal is attached with a stored reason, or not at all** (DEC-4 option B).
  :func:`link_signals` collects candidates mechanically — a new piece that
  clusters with an issue's own signals — and a model decides membership and
  writes one sentence why. An assignment the model cannot justify is not
  stored; the CHECK on ``issue_signals.reason`` holds that at the schema and
  :func:`attach` holds it at the door.
* **Wahrscheinlichkeit and Wirkung are suggested and set by a person.**
  :func:`suggest` is arithmetic over the attached signals and is never written
  to the row; :func:`grade` writes what a person chose, with the person beside
  it — a model-set probability looks like a measurement and is an opinion.
* **Escalation is a handover, not an end.** :func:`escalate` declares the crisis
  off the issue's newest article signal and links the two, so the crisis's
  chronology can begin on the day the first signal arrived rather than on the
  day somebody pressed the button (:func:`newspulse.crisis.prehistory` is the
  read side).

The model call in :func:`link_signals` is the only one in this module, it is
injectable, and no test exercises it against a real backend.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from importlib import resources
from string import Template

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import brain, config, outlets, prose
from . import crisis as crisis_mod
from .analyzer import ParseError, invoke_with_fallback, strip_code_fence
from .models import (
    CRISIS_DECLARED_BY_MAX,
    ISSUE_SCALE_MAX,
    ISSUE_SCALE_MIN,
    Analysis,
    Article,
    Client,
    Crisis,
    Issue,
    IssueDismissal,
    IssueSignal,
    IssueStatus,
    MarketSignal,
    Tonality,
    visible_coverage,
)
from .stories import cluster

_log = logging.getLogger(__name__)

_PROMPT_RESOURCE = "prompts/issue_link.txt"

#: What ``attached_by`` holds for a DEC-4 assignment: the model decided and
#: wrote the sentence. Distinct from the ``"mensch"`` token on purpose — the
#: register shows who hung a signal on the row, because a model's one-sentence
#: reason reads differently from a consultant's.
ATTACHED_BY_MODEL = "modell"

#: How far back a repetition is looked for, in days. The same seven the band's
#: own repetition (:data:`newspulse.reputation.REPETITION_DAYS`) reads over, and
#: for the same reason: a matter being carried shows inside a week, and two
#: unrelated bad Tuesdays two months apart are not one matter.
REPETITION_DAYS = 7

#: How far back a *dated* market signal is read when the repetition's second
#: half is one. Wider than the coverage window because a consultation found
#: three weeks ago is still the same matter when the press picks it up today —
#: and bounded, because an unbounded read would drag every signal the mandate
#: ever had through the clusterer on every render.
SIGNAL_LOOKBACK_DAYS = 30

#: The window new pieces are collected over as linking candidates: the sweep's
#: own day. Anything older either went through an earlier sweep's linking or
#: predates the issue, and re-asking the model about it every morning would
#: spend a call per stale pair for ever.
LINK_WINDOW = dt.timedelta(hours=24)

#: How many of an issue's own signals the linking prompt lists, newest first.
#: The prompt exists to show what the matter is, not the whole register row.
_MAX_SIGNAL_LINES = 10


class Repetition(StrEnum):
    """What kind of repetition produced a proposal.

    Carried on the proposal because the two read differently to a person: the
    same story ran on a second day, or the press and the regulatory calendar
    arrived at the same matter. Not stored — a proposal writes nothing.
    """

    ZWEITER_TAG = "zweiter_tag"
    MARKTSIGNAL = "marktsignal"


@dataclass(frozen=True, slots=True)
class IssueProposal:
    """An offer to open an issue, and nothing else. Never stored.

    ``article_ids`` is every article of the repetition, because accepting means
    attaching all of them: the value is in the attaching, and an issue opened
    with one signal out of four would start life understating its own matter.
    """

    client_id: int
    article_id: int
    headline: str
    kind: Repetition
    #: Distinct local days the story's coverage falls on.
    days: int
    #: Distinct outlets carrying the story — the plain pickup count.
    outlets: int
    article_ids: tuple[int, ...]
    signal_id: int | None
    signal_title: str


@dataclass(frozen=True, slots=True)
class Suggestion:
    """The suggested grading, with the counts it was suggested from.

    Never written to the row. The counts travel with the two numbers so the
    person who sets the real value can see what the suggestion rests on — the
    same reason the crisis level keeps its four counts beside it.
    """

    probability: int
    impact: int
    days: int
    outlets: int
    national: bool


@dataclass(frozen=True, slots=True)
class _Row:
    """One stored piece — article or market signal — in the clusterer's shape.

    ``headline``/``source``/``importance`` are the clusterer's protocol; exactly
    one of ``article``/``signal`` rides along, with the piece's own date, so no
    caller looks the row up a second time.
    """

    headline: str
    source: str
    importance: int
    happened_at: dt.datetime
    article: Article | None = None
    signal: MarketSignal | None = None


# --- Reading the stored pieces ---------------------------------------------------


def _local_day(moment: dt.datetime) -> dt.date:
    """The local day ``moment`` falls on — the day the register counts in."""
    return moment.astimezone(config.local_zone()).date()


def signal_date(signal: MarketSignal) -> dt.datetime | None:
    """The date a market signal *is about*, or ``None`` for an undated one.

    Publication first — a study's actionable date — then the day it lands or
    opens, then the deadline. Never ``found_at``: when the sweep noticed it is a
    log line, and "datiertes Marktsignal" means the matter has a date, not that
    the tool has a clock.
    """
    return signal.published_at or signal.effective_at or signal.deadline_at


def _negative_rows(
    session: Session, client: Client, *, since: dt.datetime, until: dt.datetime
) -> list[_Row]:
    """The mandate's visible *negative* coverage in the window, richest first.

    Negative only, because an issue is a repeated accusation: the friendly
    write-up of the same subject is coverage, not a signal that something must
    be watched. Importance-first ordering is the clusterer's protocol — the
    first member of a story becomes its lead.
    """
    pairs = session.execute(
        select(Article, Analysis)
        .join(Analysis, Analysis.article_id == Article.id)
        .where(
            Analysis.client_id == client.id,
            Analysis.tonality == Tonality.NEGATIV,
            visible_coverage(),
            Article.published_at >= since,
            Article.published_at <= until,
        )
        .order_by(Analysis.importance_score.desc(), Article.published_at.asc())
    ).all()
    return [
        _Row(
            headline=article.title,
            source=article.source,
            importance=analysis.importance_score,
            happened_at=article.published_at,
            article=article,
        )
        for article, analysis in pairs
    ]


def _dated_signal_rows(
    session: Session, client: Client, *, since: dt.datetime
) -> list[_Row]:
    """The mandate's dated market signals the sweep found since ``since``.

    Undated ones are skipped rather than backfilled with ``found_at``: the
    acceptance says a *dated* signal, and when the tool noticed something is
    not a date the matter has.
    """
    rows = session.scalars(
        select(MarketSignal).where(
            MarketSignal.client_id == client.id,
            MarketSignal.found_at >= since,
        )
    ).all()
    return [
        _Row(
            headline=row.title,
            source=row.publisher,
            importance=0,
            happened_at=dated,
            signal=row,
        )
        for row in rows
        if (dated := signal_date(row)) is not None
    ]


def _spoken_for(
    session: Session, client: Client, article_ids: list[int]
) -> tuple[set[int], set[int]]:
    """Which of these articles are already attached to an issue, or dismissed.

    Both suppress a proposal, and both suppress the whole story they sit in —
    a repetition somebody accepted lives on its issue row, and one somebody
    waved off must not come back under a different headline. Bounded to the
    caller's own candidate ids, so nothing here walks the archive.
    """
    if not article_ids:
        return set(), set()
    attached = set(
        session.scalars(
            select(IssueSignal.article_id)
            .join(Issue, Issue.id == IssueSignal.issue_id)
            .where(
                Issue.client_id == client.id,
                IssueSignal.article_id.in_(article_ids),
            )
        ).all()
    )
    dismissed = set(
        session.scalars(
            select(IssueDismissal.article_id).where(
                IssueDismissal.client_id == client.id,
                IssueDismissal.article_id.in_(article_ids),
            )
        ).all()
    )
    return attached, dismissed


# --- The proposal (DEC-3: the tool proposes, a person opens) ----------------------


def propose(
    session: Session, client: Client, *, now: dt.datetime | None = None
) -> IssueProposal | None:
    """Offer an issue for this mandate, or ``None``. Writes nothing at all.

    A repetition is one of exactly two things, per the acceptance:

    * the same story carried on **two different days** — one cluster whose
      negative coverage falls on two distinct local days. "Zwei Stories an zwei
      Tagen" is what that is to a reader: Monday's wave and Friday's wave of
      the same accusation, which the clusterer rightly groups as one matter;
    * a story **and a dated market signal** of the same matter, clustered
      together on their own wording.

    The offer names what the repetition consists of — kind, days, outlets, the
    signal's title — because "the tool thinks something is up" is not a
    sentence anyone can accept or refuse.

    A story any of whose members is already attached to an issue proposes
    nothing (the signal belongs on that row, and :func:`link_signals` puts it
    there); one any of whose members was dismissed proposes nothing either —
    DEC-3's false alarm costs one click and stays costing one.
    """
    reference = now or dt.datetime.now(dt.UTC)
    since = reference - dt.timedelta(days=REPETITION_DAYS)
    articles = _negative_rows(session, client, since=since, until=reference)
    if not articles:
        return None
    signals = _dated_signal_rows(
        session, client, since=reference - dt.timedelta(days=SIGNAL_LOOKBACK_DAYS)
    )
    attached, dismissed = _spoken_for(
        session, client, [row.article.id for row in articles]
    )
    spoken_for = attached | dismissed

    # Articles first, so a story's lead is always an article: an offer has to
    # point at a piece of coverage, the way the crisis offer does.
    for story in cluster(articles + signals):
        members: tuple[_Row, ...] = story.members
        press = [row for row in members if row.article is not None]
        if not press or any(row.article.id in spoken_for for row in press):
            continue
        days = {_local_day(row.happened_at) for row in press}
        carriers = {
            outlets.normalize_outlet(row.source) for row in press if row.source
        }
        dated = next((row for row in members if row.signal is not None), None)
        if len(days) < 2 and dated is None:
            continue
        lead = press[0]
        return IssueProposal(
            client_id=client.id,
            article_id=lead.article.id,
            headline=lead.headline,
            kind=Repetition.ZWEITER_TAG if len(days) >= 2 else Repetition.MARKTSIGNAL,
            days=len(days),
            outlets=len(carriers),
            article_ids=tuple(row.article.id for row in press),
            signal_id=dated.signal.id if dated is not None else None,
            signal_title=dated.headline if dated is not None else "",
        )
    return None


def _named(by: str) -> str:
    """A person's name as the columns store it: trimmed, defaulted, truncated.

    Truncated rather than refused, the same trade the crisis declaration makes:
    an eighty-character ceiling may cost the tail of a sign-in name, never the
    click. The default is the ``"mensch"`` token — never a name nobody typed.
    """
    return ((by or "").strip() or crisis_mod.DECLARED_BY_DEFAULT)[
        :CRISIS_DECLARED_BY_MAX
    ]


def _founding_reason(proposal: IssueProposal) -> str:
    """Why a founding signal hangs on the fresh issue: the repetition itself.

    Stored German prose, like the model's own sentences: a reason is data on
    the row, read back verbatim, and it has to keep saying what the repetition
    was even after the proposal that named it is gone.
    """
    if proposal.kind is Repetition.ZWEITER_TAG:
        return (
            f"Teil der angenommenen Wiederholung: dieselbe Sache an "
            f"{proposal.days} Tagen in {proposal.outlets} Medien."
        )
    return (
        "Teil der angenommenen Wiederholung: Berichterstattung und datiertes "
        f"Marktsignal derselben Sache ({proposal.signal_title})."
    )


def accept(
    session: Session,
    client: Client,
    article: Article,
    *,
    by: str,
    now: dt.datetime | None = None,
) -> Issue | None:
    """Turn the standing proposal into an issue, or ``None`` for a stale click.

    Re-derived rather than trusted: the click carries only the offer's lead
    article, and the repetition is recomputed here so what gets stored is what
    stands *now*. A lead that shifted to a stronger copy of the same story
    still accepts; a proposal that dissolved while the page sat open — the
    coverage re-matched, a second tab accepted first — is a ``None`` and costs
    nothing. That second-tab case is also what makes a double accept one issue:
    the first click attaches the articles, and the re-derivation then finds
    them spoken for.

    ``opened_at`` becomes the earliest founding signal's own date — the day the
    matter began, which is what the age on the register row and the start of an
    escalated crisis's chronology are both statements about. The click's moment
    is only ``attached_at``.
    """
    proposal = propose(session, client, now=now)
    if proposal is None or article.id not in proposal.article_ids:
        return None
    reference = now or dt.datetime.now(dt.UTC)
    reason = _founding_reason(proposal)
    person = _named(by)

    rows = session.scalars(
        select(Article).where(Article.id.in_(proposal.article_ids))
    ).all()
    moments = [row.published_at for row in rows]
    signal = (
        session.get(MarketSignal, proposal.signal_id)
        if proposal.signal_id is not None
        else None
    )
    if signal is not None and (dated := signal_date(signal)) is not None:
        moments.append(dated)

    issue = Issue(
        client_id=client.id,
        title=proposal.headline,
        opened_by=person,
        opened_at=min(moments),
        last_moved_at=max(moments),
    )
    for row in rows:
        issue.signals.append(
            IssueSignal(
                article_id=row.id,
                reason=reason,
                attached_by=person,
                attached_at=reference,
                happened_at=row.published_at,
            )
        )
    if signal is not None and signal_date(signal) is not None:
        issue.signals.append(
            IssueSignal(
                signal_id=signal.id,
                reason=reason,
                attached_by=person,
                attached_at=reference,
                happened_at=signal_date(signal),
            )
        )
    session.add(issue)
    session.commit()
    _log.info(
        "issue %d opened for %r by %r with %d signal(s)",
        issue.id,
        client.name,
        person,
        len(issue.signals),
    )
    return issue


def dismiss(
    session: Session,
    client: Client,
    article: Article,
    *,
    by: str,
    now: dt.datetime | None = None,
) -> IssueDismissal:
    """Wave a proposal off: the same repetition stops being offered.

    DEC-3's one-click false alarm. Nothing else changes — no row, no rung —
    because nothing was opened: :func:`propose` reads the dismissal through the
    same clustering the offers come from, so the whole repeated story stops
    being offered, not merely the one headline that led it.

    Idempotent per (mandate, trigger), on the same two-layer promise the crisis
    dismissal keeps: the read hands the standing row back, and the UNIQUE over
    the pair catches the race the read cannot see.
    """
    standing = session.scalars(
        select(IssueDismissal).where(
            IssueDismissal.client_id == client.id,
            IssueDismissal.article_id == article.id,
        )
    ).first()
    if standing is not None:
        return standing
    dismissal = IssueDismissal(
        client_id=client.id,
        article_id=article.id,
        dismissed_by=_named(by),
        dismissed_at=now or dt.datetime.now(dt.UTC),
    )
    session.add(dismissal)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        standing = session.scalars(
            select(IssueDismissal).where(
                IssueDismissal.client_id == client.id,
                IssueDismissal.article_id == article.id,
            )
        ).first()
        if standing is None:
            raise
        return standing
    return dismissal


# --- Attaching (DEC-4: the reason is the price of admission) ----------------------


def _standing_signal(
    session: Session,
    issue: Issue,
    *,
    article: Article | None,
    signal: MarketSignal | None,
) -> IssueSignal | None:
    """The row this (issue, piece) pair already has, if any."""
    where = (
        IssueSignal.article_id == article.id
        if article is not None
        else IssueSignal.signal_id == signal.id
    )
    return session.scalars(
        select(IssueSignal).where(IssueSignal.issue_id == issue.id, where)
    ).first()


def attach(
    session: Session,
    issue: Issue,
    *,
    article: Article | None = None,
    signal: MarketSignal | None = None,
    reason: str,
    by: str,
    now: dt.datetime | None = None,
) -> IssueSignal:
    """Hang one signal on one issue, with the reason it hangs there.

    The one writer of ``issue_signals``, and the door the DEC-4 rule is held
    at: an empty reason raises rather than stores, because an assignment nobody
    can justify is not evidence of anything — the CHECK on the column holds the
    same rule against any future writer that bypasses this function.

    ``opened_at`` and ``last_moved_at`` move with the signals: they are
    statements about the matter, so a signal older than the opening extends the
    age backwards and a newer one is the last movement.

    Idempotent per (issue, piece): the read hands the standing row back, and
    the UNIQUE pair catches the race the read cannot see.
    """
    if (article is None) == (signal is None):
        raise ValueError(
            "Ein Signal ist genau ein gespeicherter Beitrag oder genau ein "
            "Marktsignal."
        )
    cleaned = prose.plain((reason or "").strip())
    if not cleaned:
        raise ValueError("Eine Zuordnung ohne Begründung wird nicht gespeichert.")
    standing = _standing_signal(session, issue, article=article, signal=signal)
    if standing is not None:
        return standing
    happened = (
        article.published_at
        if article is not None
        else signal_date(signal) or signal.found_at
    )
    row = IssueSignal(
        issue_id=issue.id,
        article_id=article.id if article is not None else None,
        signal_id=signal.id if signal is not None else None,
        reason=cleaned,
        attached_by=_named(by),
        attached_at=now or dt.datetime.now(dt.UTC),
        happened_at=happened,
    )
    issue.opened_at = min(issue.opened_at, happened)
    issue.last_moved_at = max(issue.last_moved_at, happened)
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        # One of the UNIQUEs fired: a concurrent attach won. Its row — and its
        # reason — are the record; the rollback also undid the two timestamps,
        # which the winner moved identically.
        session.rollback()
        standing = _standing_signal(session, issue, article=article, signal=signal)
        if standing is None:
            raise
        return standing
    return row


# --- The model's half of DEC-4 ----------------------------------------------------


class LinkVerdict(BaseModel):
    """The model's answer: does the piece belong, and the sentence why."""

    model_config = ConfigDict(extra="ignore")

    gehoert_dazu: bool
    begruendung: str = ""


def _parse_verdict(raw: str) -> LinkVerdict:
    """The verdict out of the model's answer, or :class:`ParseError`."""
    try:
        payload = json.loads(strip_code_fence(raw))
    except json.JSONDecodeError as exc:
        raise ParseError(f"link verdict was not valid JSON: {exc}") from exc
    try:
        return LinkVerdict.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 — pydantic raises its own type
        raise ParseError(f"link verdict did not match the schema: {exc}") from exc


def _prompt_template() -> Template:
    text = resources.files("newspulse").joinpath(_PROMPT_RESOURCE).read_text("utf-8")
    return Template(brain.compose(text))


def _signal_lines(issue: Issue) -> str:
    """The issue's own signals as the prompt shows them, newest first."""
    rows = sorted(issue.signals, key=lambda row: row.happened_at, reverse=True)
    lines = []
    for row in rows[:_MAX_SIGNAL_LINES]:
        if row.article is not None:
            lines.append(
                f"- {row.happened_at:%d.%m.%Y}: {row.article.title} "
                f"({row.article.source})"
            )
        elif row.market_signal is not None:
            lines.append(
                f"- {row.happened_at:%d.%m.%Y}: Marktsignal: "
                f"{row.market_signal.title}"
            )
    return "\n".join(lines) or "Noch keine Signale."


def _candidate_line(row: _Row) -> str:
    """The new piece as the prompt shows it — feed-provided text only."""
    what = "Marktsignal" if row.signal is not None else "Beitrag"
    return (
        f"{what} vom {row.happened_at:%d.%m.%Y}: {row.headline}"
        + (f" ({row.source})" if row.source else "")
    )


def _decide(
    client: Client, issue: Issue, candidate: _Row, invoke
) -> LinkVerdict | None:
    """One model call: does this new piece belong to this issue?

    ``None`` means the answer could not be read — nothing is stored, and the
    piece is simply not attached. Deliberately not retried here: the candidate
    ages out of :data:`LINK_WINDOW` within a day, and a repetition that matters
    will produce a fresh candidate the next morning.
    """
    prompt = _prompt_template().substitute(
        client_name=client.name,
        issue_title=issue.title,
        issue_description=(issue.description or "").strip() or "Keine Beschreibung.",
        signals=_signal_lines(issue),
        candidate=_candidate_line(candidate),
    )
    try:
        return _parse_verdict(invoke(prompt, timeout=config.ANALYZER_TIMEOUT))
    except Exception as exc:  # noqa: BLE001 — one pair must not cost the run
        _log.warning(
            "issue link verdict for %r on %r failed: %s; storing nothing",
            client.name,
            candidate.headline,
            exc,
        )
        return None


def _issue_rows(issue: Issue) -> list[_Row]:
    """An issue's article signals in the clusterer's shape, plus its title.

    The title rides along as its own row because an issue whose wording a
    person has sharpened should keep matching the matter as *they* named it,
    not only as the founding headlines did.
    """
    rows = [
        _Row(
            headline=row.article.title,
            source=row.article.source,
            importance=1,
            happened_at=row.happened_at,
            article=row.article,
        )
        for row in issue.signals
        if row.article is not None
    ]
    rows.append(
        _Row(headline=issue.title, source="", importance=1, happened_at=issue.opened_at)
    )
    return rows


def _link_candidates(
    session: Session, client: Client, *, since: dt.datetime
) -> list[_Row]:
    """The new pieces of the window that no issue of this mandate holds yet.

    Any tonality, unlike the proposal's rows: whether a neutral follow-up
    belongs to a running matter is exactly the question the model is asked, and
    pre-filtering it away would blind DEC-4 to the common case.
    """
    already = (
        select(IssueSignal.article_id)
        .join(Issue, Issue.id == IssueSignal.issue_id)
        .where(Issue.client_id == client.id)
        .scalar_subquery()
    )
    pairs = session.execute(
        select(Article, Analysis)
        .join(Analysis, Analysis.article_id == Article.id)
        .where(
            Analysis.client_id == client.id,
            visible_coverage(),
            Article.published_at >= since,
            Article.id.not_in(already),
        )
        .order_by(Article.published_at.asc())
    ).all()
    rows = [
        _Row(
            headline=article.title,
            source=article.source,
            importance=0,
            happened_at=article.published_at,
            article=article,
        )
        for article, _analysis in pairs
    ]
    taken = (
        select(IssueSignal.signal_id)
        .join(Issue, Issue.id == IssueSignal.issue_id)
        .where(Issue.client_id == client.id)
        .scalar_subquery()
    )
    fresh = session.scalars(
        select(MarketSignal).where(
            MarketSignal.client_id == client.id,
            MarketSignal.found_at >= since,
            MarketSignal.id.not_in(taken),
        )
    ).all()
    rows += [
        _Row(
            headline=row.title,
            source=row.publisher,
            importance=0,
            happened_at=dated,
            signal=row,
        )
        for row in fresh
        if (dated := signal_date(row)) is not None
    ]
    return rows


def link_signals(
    session: Session,
    client: Client,
    *,
    invoke=None,
    now: dt.datetime | None = None,
) -> int:
    """Attach the day's new pieces to the mandate's open issues (DEC-4 option B).

    Returns how many signals were attached. The mechanics collect the
    candidates — a new piece that clusters with an issue's own signals or its
    title — and the model decides membership and writes the sentence why. Three
    outcomes per pair, and only one of them writes:

    * the model says yes with a reason — attached, ``attached_by = "modell"``;
    * the model says yes without a reason — **not stored**, and said in the
      log: an assignment nobody can justify is not evidence;
    * the model says no, or cannot be read — nothing, and nothing to undo.

    A candidate attached to one issue is not offered to the next: the whole
    point is one row per matter, and a piece that founded two rows would be the
    duplication this register exists to end.
    """
    open_rows = open_issues(session, client)
    if not open_rows:
        return 0
    reference = now or dt.datetime.now(dt.UTC)
    candidates = _link_candidates(session, client, since=reference - LINK_WINDOW)
    if not candidates:
        return 0
    resolved_invoke = invoke if invoke is not None else invoke_with_fallback
    attached = 0
    placed: set[int] = set()  # positions in ``candidates`` already attached
    for issue in open_rows:
        own = _issue_rows(issue)
        pending = [
            (index, row)
            for index, row in enumerate(candidates)
            if index not in placed
        ]
        if not pending:
            break
        grouped = cluster(own + [row for _index, row in pending])
        mechanical = _mechanical_matches(grouped, own, pending)
        for index, row in mechanical:
            verdict = _decide(client, issue, row, resolved_invoke)
            if verdict is None or not verdict.gehoert_dazu:
                continue
            reason = prose.plain(verdict.begruendung.strip())
            if not reason:
                _log.warning(
                    "the model attached %r to issue %d without a reason; "
                    "an unjustifiable assignment is not stored",
                    row.headline,
                    issue.id,
                )
                continue
            attach(
                session,
                issue,
                article=row.article,
                signal=row.signal,
                reason=reason,
                by=ATTACHED_BY_MODEL,
                now=reference,
            )
            placed.add(index)
            attached += 1
    return attached


def _mechanical_matches(
    grouped, own: list[_Row], pending: list[tuple[int, _Row]]
) -> list[tuple[int, _Row]]:
    """The candidates that landed in a story with one of the issue's own rows.

    The mechanical half of DEC-4: cheap, deterministic, and deliberately only a
    collector — a candidate surviving this is a *question*, never an answer.
    """
    own_ids = {id(row) for row in own}
    matches = []
    for story in grouped:
        members = list(story.members)
        if not any(id(member) in own_ids for member in members):
            continue
        matches += [
            (index, row)
            for index, row in pending
            if any(member is row for member in members)
        ]
    return matches


# --- Grading (suggested by arithmetic, set by a person) ---------------------------

#: What national reach adds to the suggested Wirkung. Two, the same weight the
#: crisis level and the reading give it: where a story ran decides who reads it.
_IMPACT_NATIONAL = 2

#: What a story three outlets carry adds. One — the same wave floor the crisis
#: proposal counts from.
_IMPACT_WAVE_OUTLETS = 3

#: The outlet tier that counts as national reach (the Leitmedien list).
_NATIONAL_TIER = 1


def suggest(issue: Issue) -> Suggestion:
    """The suggested grading, counted from the attached signals. Never stored.

    Arithmetic a person can re-derive, and a person overrides: the days a
    matter has recurred on suggest how likely it is to keep coming
    (Wahrscheinlichkeit), and its reach suggests what it costs if it does
    (Wirkung). Both clamped onto the register's own scale.
    """
    days = {_local_day(row.happened_at) for row in issue.signals}
    sources = {
        outlets.normalize_outlet(row.article.source)
        for row in issue.signals
        if row.article is not None and row.article.source
    }
    national = any(
        outlets.tier_for(row.article.source) == _NATIONAL_TIER
        for row in issue.signals
        if row.article is not None
    )
    probability = max(ISSUE_SCALE_MIN, min(ISSUE_SCALE_MAX, len(days)))
    impact = min(
        ISSUE_SCALE_MAX,
        ISSUE_SCALE_MIN
        + (_IMPACT_NATIONAL if national else 0)
        + (1 if len(sources) >= _IMPACT_WAVE_OUTLETS else 0),
    )
    return Suggestion(
        probability=probability,
        impact=impact,
        days=len(days),
        outlets=len(sources),
        national=national,
    )


def grade(
    session: Session,
    issue: Issue,
    *,
    by: str,
    probability: int | None = None,
    impact: int | None = None,
) -> Issue:
    """A person sets Wahrscheinlichkeit and/or Wirkung, and the row says who.

    ``None`` leaves a value untouched — the form posts one or both. Out of the
    1-5 scale raises rather than clamps: a clamped grade would store a number
    the person did not choose, under their name.
    """
    for value in (probability, impact):
        if value is not None and not ISSUE_SCALE_MIN <= value <= ISSUE_SCALE_MAX:
            raise ValueError(
                f"Eine Bewertung liegt zwischen {ISSUE_SCALE_MIN} und "
                f"{ISSUE_SCALE_MAX}."
            )
    person = _named(by)
    if probability is not None:
        issue.probability = probability
        issue.probability_set_by = person
    if impact is not None:
        issue.impact = impact
        issue.impact_set_by = person
    session.commit()
    return issue


def update_details(
    session: Session,
    issue: Issue,
    *,
    description: str | None = None,
    early_indicators: str | None = None,
    owner: str | None = None,
) -> Issue:
    """The three free-text fields a person maintains. ``None`` leaves one be."""
    if description is not None:
        issue.description = description.strip()
    if early_indicators is not None:
        issue.early_indicators = early_indicators.strip()
    if owner is not None:
        issue.owner = owner.strip()[:CRISIS_DECLARED_BY_MAX]
    session.commit()
    return issue


# --- Escalating and closing -------------------------------------------------------


def escalate(
    session: Session,
    issue: Issue,
    *,
    by: str,
    now: dt.datetime | None = None,
) -> Crisis:
    """Declare the crisis this issue became, and hand it the prehistory.

    The crisis is declared off the issue's newest article signal — a crisis
    needs a trigger article to be graded and explained — and the issue is
    marked ``eskaliert`` with ``crisis_id`` pointing at it. That link is what
    lets the crisis's chronology begin on the day the first signal arrived
    (:func:`newspulse.crisis.prehistory` reads it back): the matter did not
    start on the day somebody pressed this button.

    Idempotent: an issue that already escalated hands back its crisis. A closed
    issue refuses — it was decided to stop watching, and undoing that decision
    is a reopening, not a side effect of a button. An issue with no article
    signal refuses too, because a crisis without a trigger article cannot exist
    (the schema says so), and pretending one of the market signals is coverage
    would grade the crisis off a row that is not press.
    """
    if issue.crisis_id is not None:
        standing = session.get(Crisis, issue.crisis_id)
        if standing is not None:
            return standing
    if issue.closed_at is not None:
        raise ValueError("Ein geschlossenes Issue eskaliert nicht.")
    newest = max(
        (row for row in issue.signals if row.article is not None),
        key=lambda row: row.happened_at,
        default=None,
    )
    if newest is None:
        raise ValueError(
            "Ohne Beitrag als Signal lässt sich keine Krise erklären: eine "
            "Krise braucht den Beitrag, an dem sie hängt."
        )
    client = session.get(Client, issue.client_id)
    declared = crisis_mod.declare(session, client, newest.article, by=by, now=now)
    issue.status = IssueStatus.ESKALIERT
    issue.crisis_id = declared.id
    session.commit()
    _log.info("issue %d escalated to crisis %d", issue.id, declared.id)
    return declared


def close(
    session: Session,
    issue: Issue,
    *,
    reason: str,
    by: str,
    now: dt.datetime | None = None,
) -> Issue:
    """End an issue, keeping the row, its signals and the reason it ended.

    The reason is required here rather than at the button, the same discipline
    the crisis keeps: "why did we stop watching this" answered with an empty
    string is silence three months later. Idempotent — closing a closed issue
    keeps the first reason and the first timestamp, because those are what
    happened.
    """
    cleaned = (reason or "").strip()
    if not cleaned:
        raise ValueError("Ein Issue wird nur mit Begründung geschlossen.")
    if issue.closed_at is not None:
        return issue
    issue.closed_at = now or dt.datetime.now(dt.UTC)
    issue.close_reason = cleaned
    issue.closed_by = _named(by)
    issue.status = IssueStatus.GESCHLOSSEN
    session.commit()
    _log.info("issue %d closed: %s", issue.id, cleaned)
    return issue


# --- Reading the register ---------------------------------------------------------


def open_issues(session: Session, client: Client) -> list[Issue]:
    """The mandate's open issues, most urgent first.

    Urgency is the graded pair where both values stand — probability times
    impact, the same product the heatmap plots — and the last movement breaks
    ties. An ungraded issue sorts by movement alone: it is not "urgency zero",
    it is ungraded, and the heatmap says that in a named column.
    """
    rows = session.scalars(
        select(Issue).where(
            Issue.client_id == client.id, Issue.status == IssueStatus.OFFEN
        )
    ).all()
    return sorted(
        rows,
        key=lambda row: (
            (row.probability or 0) * (row.impact or 0),
            row.last_moved_at,
        ),
        reverse=True,
    )


def has_open_issue(session: Session, client: Client) -> bool:
    """Whether any issue of this mandate stands open — what lifts the band's
    rung to Issue (see :func:`newspulse.reputation.measure`)."""
    return (
        session.execute(
            select(Issue.id)
            .where(Issue.client_id == client.id, Issue.status == IssueStatus.OFFEN)
            .limit(1)
        ).first()
        is not None
    )


def history(session: Session, client: Client) -> list[Issue]:
    """Every issue this mandate ever had, newest opening first.

    Open, escalated and closed alike: a closed issue stays readable with all
    its signals — the register is the memory this feature exists to be, and
    hiding a closed row would delete it.
    """
    return list(
        session.scalars(
            select(Issue)
            .where(Issue.client_id == client.id)
            .order_by(Issue.opened_at.desc())
        ).all()
    )


__all__ = [
    "ATTACHED_BY_MODEL",
    "LINK_WINDOW",
    "REPETITION_DAYS",
    "SIGNAL_LOOKBACK_DAYS",
    "IssueProposal",
    "LinkVerdict",
    "Repetition",
    "Suggestion",
    "accept",
    "attach",
    "close",
    "dismiss",
    "escalate",
    "grade",
    "has_open_issue",
    "history",
    "link_signals",
    "open_issues",
    "propose",
    "signal_date",
    "suggest",
    "update_details",
]
