"""A crisis as a row: when it began, how bad it is, who said so, and when it ended.

Until now a crisis was a red card on Today. The analyzer wrote ``category =
krise``, the alert rail lit up, and after that the consultant was on their own —
nothing in the tool knew that a mandate *was in* one, so nothing could behave
differently while it lasted.

This module is that missing object, and it is deliberately small. It holds four
things and no more:

* :func:`propose` — the two stored conditions under which the tool *offers* a
  crisis. It writes nothing. DEC-1 locked option A: the tool proposes and a
  person declares, because a false alarm should cost one click rather than a
  whole morning in emergency mode. So a proposal leaves the cadence exactly as it
  was, writes no text, and adds no notification beyond the alerting that already
  fires.
* :func:`declare` / :func:`close` — the two writes, and the only two.
* :func:`severity`, and :func:`regrade` which stores what it computed — the
  level. See below.
* :func:`due` / :func:`mark_swept` — the state the tighter cadence reads, which
  lives on the row and never in the memory of the thread that swept last.

Why the level is arithmetic and not a judgement
-----------------------------------------------
A model asked for a crisis level returns a number nobody can check, and it
returns it in exactly the hour somebody wants to check it. All four inputs are
already in stored rows — how many outlets carry the story, whether any of them
is national, what share of the story reads negative for the mandate, and whether
the mandate is named — so the level is counted from them and every count is kept
on the crisis row beside the result. A consultant asked "why is this a 4" gets
the four numbers, not a paragraph.

Nothing here calls a model, and nothing here reaches the network.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import config, outlets
from .matching import mentions_client, name_matcher
from .models import (
    CRISIS_DECLARED_BY_MAX,
    CRISIS_LEVEL_MAX,
    CRISIS_LEVEL_MIN,
    Analysis,
    Article,
    Category,
    Client,
    Crisis,
    Tonality,
    visible_coverage,
)
from .stories import cluster

_log = logging.getLogger(__name__)

#: What :attr:`~newspulse.models.Crisis.declared_by` holds when the tool has no
#: better name for the person who pressed the button — the same token
#: ``ClientFact.filled_by`` and ``Outreach.released_by`` use. Never a name nobody
#: typed: DEC-1 turns on a human having decided, so the row says a human did.
DECLARED_BY_DEFAULT = "mensch"


# --- The two conditions that produce a proposal --------------------------------

#: The importance at which a single ``krise`` analysis is enough on its own. Eight
#: of ten is above the alert threshold (7) by a clear margin: a story the analyzer
#: both filed as a crisis *and* rated near the top of its scale is not a bad news
#: day, and it is worth putting the question to a person.
PROPOSAL_IMPORTANCE = 8

#: How many distinct outlets carrying the same story negatively make the second
#: condition. Three is where syndication stops looking like one outlet's angle:
#: two can be a wire copy and its pickup, three is a wave.
PROPOSAL_OUTLETS = 3

#: The window both conditions are read over. A proposal belongs to the morning it
#: is shown on, and "drei Medien innerhalb von 24 Stunden" is the acceptance
#: criterion the second condition is written from.
_PROPOSAL_WINDOW = dt.timedelta(hours=24)

#: How far *before* its triggering article a story is read together. The trigger
#: anchors the window rather than "now", so re-grading a crisis on its third day
#: still sees the coverage that started it — a window hung off the clock would
#: quietly forget the worst of it and lower the level as the crisis aged.
#:
#: Public because ``job.run_crisis`` reads its feeds over the same span and the
#: two must not drift: a lookback shorter than this window would leave the level
#: counting coverage the reading never had a chance to fetch.
STORY_WINDOW = dt.timedelta(hours=24)


class Trigger(StrEnum):
    """Which of the two conditions produced a proposal.

    Carried on the proposal because the two read differently to a person: one
    analysis called it a crisis, or nobody called it anything and it is
    everywhere. Not stored — a proposal writes nothing.
    """

    KATEGORIE = "kategorie"
    WELLE = "welle"


# --- The level ------------------------------------------------------------------

#: Outlet-count buckets, richest first, as ``(at least this many, points)``. The
#: three-outlet step is the same wave threshold the proposal uses, so a crisis
#: declared off condition two starts at one point rather than zero.
_OUTLET_POINTS: tuple[tuple[int, int], ...] = ((10, 3), (5, 2), (3, 1))

#: What national reach adds. Two, the same as a fully negative story, because
#: where a story ran decides who reads it — a tier-1 outlet sets the agenda the
#: regional press then follows (see :mod:`newspulse.outlets`).
_NATIONAL_POINTS = 2

#: The outlet tier that counts as national reach. Tier 1 is the Leitmedien list
#: in ``outlet_tiers.toml``; everything else is regional, trade or wire.
_NATIONAL_TIER = 1

#: Negative-share buckets, as ``(at least this share, points)``. Four fifths is
#: "the coverage is against us"; half is "it is contested".
_NEGATIVE_POINTS: tuple[tuple[float, int], ...] = ((0.8, 2), (0.5, 1))

#: What being named is worth. One point, not more: a story the mandate is named
#: in is worse than one it is not, but naming alone is a mention, not a crisis.
_NAMED_POINTS = 1

#: Points to level, richest first. Eight points is every input at its maximum —
#: ten outlets, national, four fifths negative, named — so level 5 is the full
#: house and nothing less. Below the last threshold the level is
#: :data:`LEVEL_MIN`: a declared crisis is never level zero.
_LEVEL_POINTS: tuple[tuple[int, int], ...] = ((8, 5), (6, 4), (4, 3), (2, 2))

#: The scale, under this module's own names. The bounds themselves are a schema
#: fact — the CHECK on ``crises.level`` — and there must be exactly one of them,
#: or the day they are widened the arithmetic and the database disagree about
#: what a level is.
LEVEL_MIN = CRISIS_LEVEL_MIN
LEVEL_MAX = CRISIS_LEVEL_MAX


@dataclass(frozen=True, slots=True)
class Severity:
    """A level and the four counts it was computed from.

    The counts travel with the number on purpose. They are what makes the level
    checkable a week later, and they are stored on the crisis row for the same
    reason.
    """

    outlets: int
    articles: int
    negative: int
    national: bool
    named: bool
    points: int
    level: int

    @property
    def negative_share(self) -> float:
        """The share of the story that reads negative for the mandate."""
        return self.negative / self.articles if self.articles else 0.0


@dataclass(frozen=True, slots=True)
class Proposal:
    """An offer to declare, and nothing else. Never stored."""

    client_id: int
    article_id: int
    trigger: Trigger
    headline: str
    outlets: int


@dataclass(frozen=True, slots=True)
class _Row:
    """One stored piece of coverage in the shape :mod:`newspulse.stories` needs.

    ``headline``/``source``/``importance`` are the clusterer's protocol; the
    article and its tonality ride along so the caller does not have to look the
    row up a second time.
    """

    headline: str
    source: str
    importance: int
    article: Article
    tonality: Tonality
    category: Category


# --- Reading the stored coverage -----------------------------------------------


def _rows(
    session: Session,
    client: Client,
    *,
    since: dt.datetime,
    until: dt.datetime | None = None,
) -> list[_Row]:
    """This mandate's visible coverage published since ``since``, richest first.

    Ordered by importance because :func:`~newspulse.stories.cluster` takes input
    order as the ranking and makes the first member of a story its lead, so the
    lead is the strongest copy rather than whichever one the database returned.

    ``until`` closes the window at the top. It is left open by default because
    the live callers want it open — a crisis that is still spreading has to keep
    counting the pickups arriving now — and it is set only where a *bounded*
    event is the question (see :func:`_stood_down`).
    """
    window = [Article.published_at >= since]
    if until is not None:
        window.append(Article.published_at <= until)
    pairs = session.execute(
        select(Article, Analysis)
        .join(Analysis, Analysis.article_id == Article.id)
        .where(
            Analysis.client_id == client.id,
            visible_coverage(),
            *window,
        )
        .order_by(Analysis.importance_score.desc(), Article.published_at.asc())
    ).all()
    return [
        _Row(
            headline=article.title,
            source=article.source,
            importance=analysis.importance_score,
            article=article,
            tonality=analysis.tonality,
            category=analysis.category,
        )
        for article, analysis in pairs
    ]


def _row_for(session: Session, client: Client, article: Article) -> _Row:
    """One row for ``article``, whether or not it carries an analysis.

    A crisis can be declared off an article this mandate has no analysis for —
    dismissed, or declared by hand from the archive — and refusing to grade it
    would be worse than grading it as the single unknown-tonality story it is.
    """
    analysis = session.scalars(
        select(Analysis).where(
            Analysis.article_id == article.id, Analysis.client_id == client.id
        )
    ).first()
    return _Row(
        headline=article.title,
        source=article.source,
        importance=analysis.importance_score if analysis else 0,
        article=article,
        tonality=analysis.tonality if analysis else Tonality.UNBEKANNT,
        category=analysis.category if analysis else Category.SONSTIGES,
    )


def _story_rows(
    session: Session,
    client: Client,
    article: Article,
    *,
    until: dt.datetime | None = None,
) -> list[_Row]:
    """Every stored row that reports the same event as ``article``.

    The window is hung off the trigger's publication rather than off the clock
    (see :data:`STORY_WINDOW`), so re-grading a crisis on its third day still
    reads the coverage that started it.

    ``until`` closes it at the top, and the default leaves it open on purpose:
    :func:`severity` must keep counting the pickups that arrive while a crisis
    runs, or a crisis would stop getting worse the moment it was declared.

    The trigger is always a member of its own story, even when it is older than
    the window or its analysis was dismissed or never written.
    """
    rows = _rows(
        session, client, since=article.published_at - STORY_WINDOW, until=until
    )
    if not any(row.article.id == article.id for row in rows):
        rows = [_row_for(session, client, article), *rows]
    for story in cluster(rows):
        members: tuple[_Row, ...] = story.members
        if any(row.article.id == article.id for row in members):
            return list(members)
    # Not reachable: ``cluster`` puts every input into exactly one group and the
    # trigger is guaranteed to be one of the inputs. The honest fallback anyway,
    # because every caller here needs a non-empty story to count.
    return [_row_for(session, client, article)]


# --- The arithmetic -------------------------------------------------------------


def _bucket(value: float, buckets: tuple[tuple[float, int], ...]) -> int:
    """The points of the first bucket ``value`` reaches, or zero."""
    for floor, points in buckets:
        if value >= floor:
            return points
    return 0


def severity(session: Session, client: Client, article: Article) -> Severity:
    """The crisis level of the story ``article`` belongs to, and its four counts.

    Counted, never estimated, and counted from rows that are already stored: how
    many outlets ran it, whether any of them is national, how much of it reads
    negative for the mandate, and whether the mandate is named in the
    feed-provided text (title plus snippet — no body is ever fetched).
    """
    rows = _story_rows(session, client, article)
    matcher = name_matcher(client)
    distinct = {outlets.normalize_outlet(row.source) for row in rows if row.source}
    national = any(outlets.tier_for(row.source) == _NATIONAL_TIER for row in rows)
    negative = sum(1 for row in rows if row.tonality is Tonality.NEGATIV)
    named = any(mentions_client(row.article, matcher) for row in rows)
    share = negative / len(rows) if rows else 0.0

    points = (
        _bucket(len(distinct), _OUTLET_POINTS)
        + (_NATIONAL_POINTS if national else 0)
        + _bucket(share, _NEGATIVE_POINTS)
        + (_NAMED_POINTS if named else 0)
    )
    level = _bucket(points, _LEVEL_POINTS) or LEVEL_MIN
    return Severity(
        outlets=len(distinct),
        articles=len(rows),
        negative=negative,
        national=national,
        named=named,
        points=points,
        level=level,
    )


# --- The proposal ---------------------------------------------------------------


def _by_category(rows: list[_Row]) -> _Row | None:
    """The first row the analyzer filed as a crisis at or above the importance."""
    for row in rows:
        if row.category is Category.KRISE and row.importance >= PROPOSAL_IMPORTANCE:
            return row
    return None


#: One clustered story as this module passes it around: its lead, and every row
#: that reports the same event. Named because the clustering is done exactly once
#: per :func:`propose` and the same grouping then answers both conditions *and*
#: supplies the pickup count.
_Story = tuple[_Row, tuple[_Row, ...]]


def _by_wave(stories: list[_Story]) -> _Story | None:
    """The first story at least :data:`PROPOSAL_OUTLETS` outlets carry negatively,
    led by the strongest negative copy.

    Only the negative members count, towards the total and towards the lead:
    three outlets on a story two of them praise is not a wave against the
    mandate, and pointing the proposal at the one approving write-up would read
    as nonsense. The *story* handed back is still the whole story, because that
    is what the pickup count on the offer means.
    """
    for _lead, members in stories:
        against = [row for row in members if row.tonality is Tonality.NEGATIV]
        carriers = {
            outlets.normalize_outlet(row.source) for row in against if row.source
        }
        if len(carriers) >= PROPOSAL_OUTLETS:
            return against[0], members
    return None


def _story_of(stories: list[_Story], row: _Row) -> tuple[_Row, ...]:
    """The clustered story ``row`` belongs to, out of the grouping already made."""
    for _lead, members in stories:
        if any(member.article.id == row.article.id for member in members):
            return members
    return (row,)


def _proposal(
    client: Client, row: _Row, trigger: Trigger, members: tuple[_Row, ...]
) -> Proposal:
    """A proposal for ``row``, carrying how far the story it belongs to has run.

    ``outlets`` is the story's plain pickup count in both cases — the number a
    reader would count on the page. Which of the two conditions produced the
    offer is what ``trigger`` says; overloading the count to mean "negative
    carriers" in one case and "all carriers" in the other would put two different
    numbers under one label.

    ``members`` comes from the *same* clustering that answered the condition, and
    that is the point rather than an economy: re-clustering the story over a
    window hung off the trigger's publication rather than off the clock produced
    a different grouping, so the number on the offer could disagree with the one
    that met the threshold.
    """
    carriers = {
        outlets.normalize_outlet(member.source) for member in members if member.source
    }
    return Proposal(
        client_id=client.id,
        article_id=row.article.id,
        trigger=trigger,
        headline=row.headline,
        outlets=len(carriers),
    )


def _stood_down(
    session: Session, client: Client, rows: list[_Row], *, since: dt.datetime
) -> set[int]:
    """The article ids belonging to a story this mandate has already stood down.

    DEC-1's whole rationale is that a false alarm costs one click. Suppressing a
    proposal only while a crisis is *open* spends that click again on every page
    load for the next twenty-four hours, because the article that triggered it is
    still inside :data:`_PROPOSAL_WINDOW` — so a stood-down story stops
    proposing, not merely the row it was declared off.

    The whole story rather than the trigger alone: the pickups are what the wave
    condition counts, and leaving them behind would re-offer the same event under
    a different headline the moment the trigger aged out.

    Deliberately not "no proposals for N hours". A different story breaking the
    same afternoon is a different question and still gets asked.

    **A stood-down story is an event, and an event ends.**
    :func:`~newspulse.stories.cluster` groups on headline similarity alone and
    has no notion of time, so an unbounded reach here would group February's
    stood-down coverage with an identically-shaped wave breaking in August and
    silence the second one for ever — the worst failure this module can have,
    because it is silent and it gets likelier the longer the tool is used. Two
    bounds keep the suppression the length of the event that earned it:

    * only crises whose trigger is recent enough that some member of its story
      can still be inside the caller's window are read at all. A story's last
      possible member lands one :data:`STORY_WINDOW` after its trigger, so a
      trigger older than ``since - STORY_WINDOW`` cannot silence anything;
    * each story is collected over its own window rather than over everything
      published since, so today's coverage cannot join a months-old event.

    Together they also keep this off the archive: the daily page render used to
    load and cluster every row published since the oldest crisis this mandate
    ever had, on every render, for ever.
    """
    reach = since - STORY_WINDOW
    triggers = session.scalars(
        select(Article)
        .join(Crisis, Crisis.article_id == Article.id)
        .where(
            Crisis.client_id == client.id,
            Crisis.closed_at.is_not(None),
            Article.published_at >= reach,
        )
    ).all()
    if not triggers:
        return set()
    visible = {row.article.id for row in rows}
    silenced: set[int] = set()
    for trigger in triggers:
        silenced.update(
            row.article.id
            for row in _story_rows(
                session,
                client,
                trigger,
                until=trigger.published_at + STORY_WINDOW,
            )
        )
    # Only the ones still in view matter; a trigger that has aged out of the
    # window costs nothing to carry and nothing to drop.
    return silenced & visible


def propose(
    session: Session, client: Client, *, now: dt.datetime | None = None
) -> Proposal | None:
    """Offer a crisis for this mandate, or ``None``. Writes nothing at all.

    Two conditions, read in the order they are worth trusting: one analysis that
    calls this a crisis and rates it at least :data:`PROPOSAL_IMPORTANCE`, or
    :data:`PROPOSAL_OUTLETS` outlets carrying the same story negatively inside
    :data:`_PROPOSAL_WINDOW`.

    A mandate that is already in a declared crisis gets no proposal: there is at
    most one open crisis per mandate, so there is nothing left to offer. Neither
    does a story somebody has already stood down — see :func:`_stood_down`.
    """
    reference = now or dt.datetime.now(dt.UTC)
    if open_crisis(session, client) is not None:
        return None
    since = reference - _PROPOSAL_WINDOW
    rows = _rows(session, client, since=since)
    silenced = _stood_down(session, client, rows, since=since)
    rows = [row for row in rows if row.article.id not in silenced]
    if not rows:
        return None

    # Clustered once, and every answer below comes out of this one grouping.
    stories: list[_Story] = [(story.lead, story.members) for story in cluster(rows)]
    flagged = _by_category(rows)
    if flagged is not None:
        return _proposal(
            client, flagged, Trigger.KATEGORIE, _story_of(stories, flagged)
        )
    wave = _by_wave(stories)
    if wave is not None:
        lead, members = wave
        return _proposal(client, lead, Trigger.WELLE, members)
    return None


# --- Declaring, closing, and the state the cadence reads ------------------------


def open_crisis(session: Session, client: Client) -> Crisis | None:
    """This mandate's open crisis, or ``None``. At most one exists by index."""
    return session.scalars(
        select(Crisis).where(
            Crisis.client_id == client.id, Crisis.closed_at.is_(None)
        )
    ).first()


def still_open(session: Session, crisis: Crisis) -> bool:
    """Whether ``crisis`` is *still* open — read from the table, not the object.

    A crisis reading is minutes long: feed fetches, then a model call per batch.
    The object it started with was loaded before any of that, so asking it
    answers a question about the past. Somebody pressing "stand down" during
    those minutes is exactly the case the caller has to notice, and the only
    place that fact exists is the row.

    A crisis whose row is gone reads as not open, which is the honest answer for
    a caller deciding whether to write to it.
    """
    row = session.execute(
        select(Crisis.closed_at).where(Crisis.id == crisis.id)
    ).first()
    return row is not None and row[0] is None


def open_crises(session: Session) -> list[Crisis]:
    """Every open crisis in the portfolio, oldest first."""
    return list(
        session.scalars(
            select(Crisis)
            .where(Crisis.closed_at.is_(None))
            .order_by(Crisis.declared_at.asc())
        ).all()
    )


def _open_on_active_mandates(session: Session) -> list[Crisis]:
    """The open crises whose mandate is still active, oldest first.

    ``active`` is this tool's kill switch for a mandate: ``clients.list_clients``
    filters on it and the daily sweep therefore never touches a deactivated one.
    The second clock has to honour the same switch, or deactivating a mandate
    while its crisis is open would leave an hourly fetch and an analyzer batch
    running against it with nothing left in the interface to stop them.

    Deliberately narrower than :func:`open_crises`, which stays the plain answer
    to "what is open" so a deactivated mandate's crisis can still be read and
    closed by a person.
    """
    return list(
        session.scalars(
            select(Crisis)
            .join(Client, Client.id == Crisis.client_id)
            .where(Crisis.closed_at.is_(None), Client.active.is_(True))
            .order_by(Crisis.declared_at.asc())
        ).all()
    )


def _grade(crisis: Crisis, computed: Severity) -> None:
    """Write a computed level and its four counts onto the row."""
    crisis.level = computed.level
    crisis.outlet_count = computed.outlets
    crisis.article_count = computed.articles
    crisis.negative_count = computed.negative
    crisis.national = computed.national
    crisis.named = computed.named


def declare(
    session: Session,
    client: Client,
    article: Article,
    *,
    by: str,
    now: dt.datetime | None = None,
) -> Crisis:
    """Declare a crisis for ``client``, or hand back the one already open.

    A second declaration is not a second crisis. The unique index over the open
    rows makes that a schema guarantee; this returns the standing row rather than
    letting the caller discover it as an ``IntegrityError``, so a double click, a
    second browser tab and a restart mid-declaration all land on the same crisis.

    The read-then-insert is not atomic, so that promise is kept twice: once by
    the read, and once by catching the index doing its job. Two connections can
    both see no open crisis before either commits — one browser tab per
    replica — and the loser of that race has to be handed the winner's crisis
    rather than a traceback.
    """
    standing = open_crisis(session, client)
    if standing is not None:
        return standing
    crisis = Crisis(
        client_id=client.id,
        article_id=article.id,
        # Truncated rather than refused: an eighty-character ceiling on a
        # sign-in name may cost the tail of the name, never the declaration.
        declared_by=((by or "").strip() or DECLARED_BY_DEFAULT)[
            :CRISIS_DECLARED_BY_MAX
        ],
        declared_at=now or dt.datetime.now(dt.UTC),
    )
    _grade(crisis, severity(session, client, article))
    session.add(crisis)
    try:
        session.commit()
    except IntegrityError:
        # ``uq_crises_one_open_per_client`` fired: somebody else declared between
        # the read above and this commit. Their row is the crisis.
        session.rollback()
        standing = open_crisis(session, client)
        if standing is None:
            raise
        _log.info(
            "a concurrent declaration for %r won; using crisis %d",
            client.name,
            standing.id,
        )
        return standing
    _log.info(
        "crisis declared for %r at level %d by %r",
        client.name,
        crisis.level,
        crisis.declared_by,
    )
    return crisis


def close(
    session: Session, crisis: Crisis, *, reason: str, now: dt.datetime | None = None
) -> Crisis:
    """End a crisis, keeping the row and the reason it ended.

    The reason is required, and it is required here rather than at the button:
    "why did we stand this down" is the first question the review asks, and an
    empty string would answer it with silence three months later.

    Idempotent — closing a closed crisis keeps the first reason and the first
    timestamp, because those are what happened.
    """
    cleaned = (reason or "").strip()
    if not cleaned:
        raise ValueError("Eine Krise wird nur mit Begründung geschlossen.")
    if crisis.closed_at is not None:
        return crisis
    crisis.closed_at = now or dt.datetime.now(dt.UTC)
    crisis.close_reason = cleaned
    session.commit()
    _log.info("crisis %d closed: %s", crisis.id, cleaned)
    return crisis


def due(session: Session, *, now: dt.datetime | None = None) -> list[Crisis]:
    """The open crises whose tighter sweep is due, read from the table.

    Never from a thread's memory. A restart, a redeploy or a crash halfway
    through a crisis run therefore loses nothing and repeats nothing: the only
    state is ``last_swept_at``, and it is stamped before the reading starts.

    A deactivated mandate is never due — see :func:`_open_on_active_mandates`.

    Its limit, stated rather than hidden: between this returning a row and
    :func:`mark_swept` committing, a second *process* could pick up the same
    crisis. The web process's run guard is in-process only by its own docstring,
    so nothing here stops that. Single-process on the deployed host today, and
    the cost if that changes is one duplicated reading rather than a wrong level
    — the reading is idempotent, and the level is recomputed from stored rows
    either way. A real fix is a row-level claim (``UPDATE … WHERE
    last_swept_at = :seen``), and it belongs with the second replica that needs
    it, not before.
    """
    reference = now or dt.datetime.now(dt.UTC)
    every = dt.timedelta(minutes=config.crisis_sweep_minutes())
    return [
        crisis
        for crisis in _open_on_active_mandates(session)
        if crisis.last_swept_at is None or reference - crisis.last_swept_at >= every
    ]


def mark_swept(
    session: Session, crisis: Crisis, *, now: dt.datetime | None = None
) -> Crisis:
    """Stamp the row *before* the reading, and commit.

    Before rather than after, and that is the whole crash story. A run that dies
    halfway has already moved the clock, so the next tick reads a crisis that is
    open and not yet due — one missed reading. Stamping afterwards would leave it
    due on every tick until something succeeded, which on a broken feed is a
    sweep a minute against the same dead source.
    """
    crisis.last_swept_at = now or dt.datetime.now(dt.UTC)
    session.commit()
    return crisis


def regrade(session: Session, crisis: Crisis) -> Severity:
    """Recompute the level from what is stored now, and keep the counts with it.

    A crisis that spreads gets worse and one that is dropped by the press does
    not, and both are read off the same stored rows the first grade was. Still
    arithmetic, still no model.
    """
    client = crisis.client or session.get(Client, crisis.client_id)
    article = crisis.article or session.get(Article, crisis.article_id)
    computed = severity(session, client, article)
    _grade(crisis, computed)
    session.commit()
    return computed


__all__ = [
    "DECLARED_BY_DEFAULT",
    "LEVEL_MAX",
    "LEVEL_MIN",
    "PROPOSAL_IMPORTANCE",
    "PROPOSAL_OUTLETS",
    "Proposal",
    "STORY_WINDOW",
    "Severity",
    "Trigger",
    "close",
    "declare",
    "due",
    "mark_swept",
    "open_crises",
    "open_crisis",
    "propose",
    "regrade",
    "severity",
    "still_open",
]
