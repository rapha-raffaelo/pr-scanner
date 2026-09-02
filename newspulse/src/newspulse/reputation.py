"""How a mandate stands, as one number a day and one line every morning.

The tool answers "what happened today" very well and has never answered the
question in front of it — how does this mandate *stand*. The only thing that
came close was the red card on Today, which counts the current day's alerts and
is gone at midnight, so the same accusation on Monday, Wednesday and Friday read
as three cards on three days rather than as one thing getting three weeks old.

This module is the missing reading, and it is deliberately small:

* :func:`measure` — the rung. Four inputs that are already in stored rows, a
  sum, and then the three things a sum alone must not decide: the brake, the
  Issue rung that a single day cannot see, and a declared crisis. It writes
  nothing.
* :func:`record` / :func:`sweep` — the two writes, and the only two. One row per
  mandate and day; a second run the same day updates it.
* :func:`direction` and :func:`deviates` — what a *series* of readings says that
  a single one cannot: which way it is moving, and whether today is unusual for
  this mandate rather than unusual in general.
* :func:`band` — the one line the morning starts with, assembled out of stored
  readings and nothing else.

Why the rung is arithmetic and not a judgement
-----------------------------------------------
DEC-2 locked option A. All four inputs are already in stored rows — how many
independent outlets carry the strongest negative story, whether any of the
negative coverage ran nationally, what share of the mandate's coverage reads
negative, and whether the mandate is named — so the rung is counted from them
and every count is kept on the reading beside the result. A consultant asked
"why is this Risiko" gets the four numbers in the hour they ask, not a paragraph
from a model that would answer differently tomorrow with no new occasion.

The rule that brakes upwards
-----------------------------
The most important rule here is the one that refuses to climb. A single negative
report never lifts a mandate above :attr:`~newspulse.models.ReputationState.
BEOBACHTUNG`, however large the outlet. Lifting it needs corroboration, and
there are exactly three kinds: two independent outlets on the same story, a
repetition on a second day, or an analysis the analyzer itself filed as
``krise`` at :data:`CORROBORATION_IMPORTANCE` or above.

Without that brake the band is decoration inside a fortnight, and it would then
have spent the attention that is needed for the one morning it is rightly red.

Nothing here calls a model, and nothing here reaches the network.
"""

from __future__ import annotations

import datetime as dt
import logging
import statistics
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import config, crisis, outlets
from .matching import mentions_client, name_matcher
from .models import (
    Analysis,
    Article,
    Category,
    Client,
    Crisis,
    ReputationReading,
    ReputationState,
    Tonality,
    visible_coverage,
)
from .stories import cluster

_log = logging.getLogger(__name__)

#: The ladder, lowest rung first. The single ordering in the codebase — every
#: comparison here reads it, so a rung is raised or lowered by moving it in this
#: tuple and nowhere else.
LADDER: tuple[ReputationState, ...] = (
    ReputationState.RUHIG,
    ReputationState.BEOBACHTUNG,
    ReputationState.ISSUE,
    ReputationState.RISIKO,
    ReputationState.KRISE,
)


def rank(state: ReputationState) -> int:
    """Where ``state`` sits on :data:`LADDER`, counting from zero."""
    return LADDER.index(state)


class Direction(StrEnum):
    """Which way a mandate has been moving over its last readings.

    A word and never a colour alone. A band that says only "red" makes the
    reader supply the sentence, and the sentence they supply is the one they
    were already expecting.
    """

    STEIGEND = "steigend"
    STABIL = "stabil"
    FALLEND = "fallend"


# --- The arithmetic's constants -------------------------------------------------

#: Outlet-count buckets for the *strongest negative story* of the window,
#: richest first, as ``(at least this many, points)``.
#:
#: One floor below the crisis's own buckets (10/5/3), which is the whole idea of
#: this module: the crisis level asks whether an event is a national wave, and
#: the reading asks whether anything is going on at all. Two outlets is where
#: the brake's first corroboration sits, so it is where the first point sits.
_OUTLET_POINTS: tuple[tuple[int, int], ...] = ((5, 3), (3, 2), (2, 1))

#: What national reach adds. Two, the same weight :mod:`newspulse.crisis` gives
#: it and for the same reason: where a story ran decides who reads it, and a
#: tier-1 outlet sets the agenda the regional press then follows.
_NATIONAL_POINTS = 2

#: The outlet tier that counts as national reach. Tier 1 is the Leitmedien list
#: in ``outlet_tiers.toml``; everything else is regional, trade or wire.
_NATIONAL_TIER = 1

#: Negative-share buckets, as ``(at least this share, points)``. Four fifths is
#: "the coverage is against us"; half is "it is contested".
_NEGATIVE_POINTS: tuple[tuple[float, int], ...] = ((0.8, 2), (0.5, 1))

#: What being named is worth. One point, not more: coverage a mandate is named
#: in is worse than coverage it is not, but naming alone is a mention.
_NAMED_POINTS = 1

#: Points to rung, richest first. Eight is every input at its maximum — five
#: outlets on one story, national, four fifths negative, named — and the top
#: rung starts one below it, at seven. Deliberately: at eight the last point
#: decides the rung, and "three outlets, national, named, wholly against us" is
#: the same morning whether the fourth outlet has filed by six or not.
#:
#: :attr:`~newspulse.models.ReputationState.ISSUE` is reached by no sum at all,
#: and that is the point of it: no count of *today's* coverage can say a matter
#: is being carried across days. It is the one rung a single day cannot see, so
#: it is read off the stored series instead — see :func:`measure`.
_STATE_POINTS: tuple[tuple[int, ReputationState], ...] = (
    (7, ReputationState.KRISE),
    (4, ReputationState.RISIKO),
    (1, ReputationState.BEOBACHTUNG),
)

#: How many independent outlets on one story corroborate a negative report. Two,
#: which is the whole of the brake's first condition: one outlet's angle is one
#: outlet's angle however loudly it is published.
CORROBORATING_OUTLETS = 2

#: The importance at which a single ``krise`` analysis corroborates on its own.
#: Deliberately the same eight :func:`newspulse.crisis.propose` uses, and it is
#: the same statement: a story the analyzer both filed as a crisis *and* rated
#: near the top of its scale is not a bad news day. Spelled here rather than
#: imported so this module's brake keeps its own number if the crisis proposal's
#: threshold ever moves for a reason that has nothing to do with the band.
CORROBORATION_IMPORTANCE = 8

#: How far back a repetition may be looked for, in days. The same seven the
#: direction is read over, so the two answers a series gives come from the same
#: stretch of history. Wider would let a single bad Tuesday in July corroborate
#: a single bad Tuesday in September, which is not a repetition — it is two
#: unrelated days two months apart.
REPETITION_DAYS = 7

#: How many readings the direction is counted over. Seven: a week, which is the
#: shortest stretch in which a German news cycle both starts and finishes.
DIRECTION_READINGS = 7

#: The fewest points an earlier day must have counted for it to be a repetition:
#: the Beobachtung floor, read off :data:`_STATE_POINTS` rather than written out
#: a second time. A day the arithmetic itself left on the bottom rung is not a
#: repetition of anything — see :func:`_repeated`.
_REPETITION_POINTS = min(floor for floor, _ in _STATE_POINTS)

#: How many readings the mandate's own baseline is taken over. Thirty, per the
#: acceptance: a mandate is measured against its own median rather than against
#: a threshold that would mean the same thing for a municipal utility and for a
#: listed group.
#:
#: This number is also *said*, in both languages, by the deviation line on the
#: tile — see the entry in :mod:`newspulse.i18n`. Moving it here means moving it
#: there, and ``test_the_deviation_sentence_names_the_baseline_it_is_counted_over``
#: is what fails if only one of the two moves.
BASELINE_READINGS = 30

#: The fewest prior readings a baseline may be claimed from. Below this there is
#: no median worth exceeding — with two readings of zero behind it, every third
#: day is "unusual for this mandate", and the band would carry that sentence for
#: every mandate in its first week and mean nothing by it ever after.
BASELINE_MIN_READINGS = 7


@dataclass(frozen=True, slots=True)
class Inputs:
    """The four stored quantities a rung is counted from.

    Two scopes, and the difference is deliberate. ``outlets``, ``national`` and
    ``named`` describe the mandate's *negative* coverage, because that is what
    the rung is about; ``articles`` is everything visible about the mandate in
    the window, because a single hostile piece inside twenty friendly ones is a
    different day from a single hostile piece on its own.

    ``outlets`` alone is story-scoped: two outlets on *the same* thing is what
    corroboration means, and two outlets on two unrelated stories is not.
    """

    outlets: int
    national: bool
    articles: int
    negative: int
    named: bool

    @property
    def negative_share(self) -> float:
        """The share of the mandate's coverage that reads negative for it."""
        return self.negative / self.articles if self.articles else 0.0


@dataclass(frozen=True, slots=True)
class Reading:
    """One computed reading: the rung, the inputs, and why it is not higher.

    ``braked`` says the arithmetic reached above Beobachtung and was held there
    because nothing corroborated a single negative report. It is the *caller's*
    answer at the moment of measuring and is deliberately not a column: the
    acceptance fixes the stored row at the state, the four inputs and the
    timestamp, and the four inputs beside a Beobachtung already say it — a rung
    below the sum they add up to is a rung that was held. Its consumers are the
    brake's tests, which assert it alongside ``points`` so that a brake achieved
    by quietly lowering the sum instead fails them.
    """

    inputs: Inputs
    points: int
    state: ReputationState
    braked: bool


@dataclass(frozen=True, slots=True)
class BandEntry:
    """One mandate's tile on the band: where it stands, and why."""

    client_id: int
    client_name: str
    state: ReputationState
    direction: Direction
    deviates: bool
    outlets: int
    national: bool
    articles: int
    negative: int
    named: bool
    day: dt.date


@dataclass(frozen=True, slots=True)
class Band:
    """The whole line: the mandates worth a tile, and a count of the rest.

    DEC-1 option B. A mandate appears only once it has left the lowest rung; the
    others are one sentence, so an ordinary morning costs the band one line and
    the morning something is wrong it is the first thing on the page.
    """

    entries: tuple[BandEntry, ...]
    quiet: int
    without_coverage: int
    day: dt.date | None

    @property
    def is_empty(self) -> bool:
        """Whether there is nothing to render at all — no mandate has a reading.

        Distinct from "everything is quiet", which is a statement and gets its
        line. This one means the sweep has never run, and a band claiming calm
        it never measured would be the worst sentence on the page.
        """
        return not self.entries and not self.quiet


# --- Reading the stored coverage -------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Row:
    """One stored piece of coverage in the shape :mod:`newspulse.stories` needs.

    ``headline``/``source``/``importance`` are the clusterer's protocol; the
    article and its analysis ride along so the caller does not look the row up a
    second time.
    """

    headline: str
    source: str
    importance: int
    article: Article
    tonality: Tonality
    category: Category


def _rows(session: Session, client: Client, *, since: dt.datetime,
          until: dt.datetime) -> list[_Row]:
    """This mandate's visible coverage published inside the window, richest first.

    Ordered by importance because :func:`~newspulse.stories.cluster` takes input
    order as the ranking, so a story's lead is its strongest copy.

    Closed at the top as well as the bottom, unlike the crisis module's open
    window: a reading is a statement about one day, and one taken with an
    injected clock has to see exactly the coverage of that day — a window left
    open would make a seeded "tomorrow" leak into a reading of today.
    """
    pairs = session.execute(
        select(Article, Analysis)
        .join(Analysis, Analysis.article_id == Article.id)
        .where(
            Analysis.client_id == client.id,
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
            article=article,
            tonality=analysis.tonality,
            category=analysis.category,
        )
        for article, analysis in pairs
    ]


def _widest_story(rows: list[_Row]) -> int:
    """How many independent outlets carry the widest of these stories.

    Zero for no rows at all. Story-scoped rather than counted over the whole
    window, because "zwei unabhängige Medien" means two on the same thing: two
    outlets on two unrelated pieces is two stories, and calling that
    corroboration is exactly the false alarm the brake exists to refuse.
    """
    return max((story.pickup_count for story in cluster(rows)), default=0)


def _inputs(client: Client, rows: list[_Row]) -> Inputs:
    """The four quantities, counted off the rows. No queries, no clock."""
    against = [row for row in rows if row.tonality is Tonality.NEGATIV]
    matcher = name_matcher(client)
    return Inputs(
        outlets=_widest_story(against),
        national=any(
            outlets.tier_for(row.source) == _NATIONAL_TIER for row in against
        ),
        articles=len(rows),
        negative=len(against),
        named=any(mentions_client(row.article, matcher) for row in against),
    )


# --- The arithmetic ---------------------------------------------------------------


def _bucket(value: float, buckets: tuple[tuple[float, int], ...]) -> int:
    """The points of the first bucket ``value`` reaches, or zero."""
    for floor, points in buckets:
        if value >= floor:
            return points
    return 0


def score(inputs: Inputs) -> int:
    """The points these four inputs sum to, between zero and eight."""
    return (
        _bucket(inputs.outlets, _OUTLET_POINTS)
        + (_NATIONAL_POINTS if inputs.national else 0)
        + _bucket(inputs.negative_share, _NEGATIVE_POINTS)
        + (_NAMED_POINTS if inputs.named else 0)
    )


def _rung(points: int) -> ReputationState:
    """The rung ``points`` reaches. ``ISSUE`` is never among them — see
    :data:`_STATE_POINTS`."""
    for floor, state in _STATE_POINTS:
        if points >= floor:
            return state
    return ReputationState.RUHIG


def _flagged(rows: list[_Row]) -> bool:
    """Whether the analyzer filed one of the *negative* rows as a crisis, high enough.

    Read over the negative coverage only, like every other input on the reading.
    The brake is holding one hostile report and this asks whether anything
    corroborates *that* report — but a piece the analyzer filed under ``krise``
    can as easily be "Solaris AG meistert die Krise", a friendly story about the
    same subject. Counting it would let good news corroborate bad, which is not
    a second source: it is one report with a second headline written over it,
    and it would carry the mandate to Risiko off a single outlet.
    """
    return any(
        row.tonality is Tonality.NEGATIV
        and row.category is Category.KRISE
        and row.importance >= CORROBORATION_IMPORTANCE
        for row in rows
    )


def _repeated(
    session: Session, client: Client, *, day: dt.date, before: dt.datetime
) -> bool:
    """Whether an earlier, *separately counted* day already stood above ruhig.

    Two things are read off this one answer: the brake's third corroboration,
    and the :attr:`~newspulse.models.ReputationState.ISSUE` rung, which is the
    same statement one rung lower — a matter that was there on a second day is
    not today's news.

    Read off the stored series rather than by re-clustering a week of coverage:
    the series is what this module writes every morning, and it is the only place
    the tool knows that something was already the matter yesterday.

    Three guards on the rows it will count, and each one is a way a row would
    otherwise say something it does not know:

    ``day`` is excluded so a second run of the same morning cannot corroborate
    itself off the row it is about to overwrite — which would turn the brake off
    for every mandate on the second run of any day.

    ``computed_at <= before`` — ``before`` being the moment the window now being
    counted opens — is that same guard across a day boundary. The window is a
    rolling stretch of hours ending at the reading's own moment, so two runs at
    different hours on two days *overlap*: one story filed once is counted in
    both readings, and the earlier row then stands as the repetition of itself.
    That is not a hypothetical — a "jetzt lesen" from the dashboard at 18:00 and
    the scheduled 06:10 run next morning are exactly it, and one report would
    reach Risiko off one outlet and no second day. Only a row whose own window
    had closed before this one opened is a second day.

    ``points`` is asked rather than ``state``, because the question is what the
    *arithmetic* said about that day. A row whose ``krise`` came from the open
    crisis floor carries zero points: a person declared it, no count reached it,
    and letting a declaration stand in as media corroboration is precisely the
    borrowed authority the brake exists to refuse. ``negative > 0`` stays beside
    it as the plainer half — there was hostile coverage at all — and together
    they are the earlier day's own answer to the question being asked of it.

    What it cannot yet say is that it was *the same* theme. That is the issue
    register's job (RIS-02), and until it exists this is honest about what a
    stored reading knows: this mandate stood above ruhig on another day this week.
    """
    floor = day - dt.timedelta(days=REPETITION_DAYS)
    return bool(
        session.execute(
            select(ReputationReading.id)
            .where(
                ReputationReading.client_id == client.id,
                ReputationReading.day < day,
                ReputationReading.day >= floor,
                ReputationReading.computed_at <= before,
                ReputationReading.negative > 0,
                ReputationReading.points >= _REPETITION_POINTS,
            )
            .limit(1)
        ).first()
    )


def _corroborated(inputs: Inputs, rows: list[_Row], *, repeated: bool) -> bool:
    """Whether anything lifts this above a single negative report.

    Three conditions, and any one of them is enough:

    * two independent outlets on the same story;
    * an analysis the analyzer filed as ``krise`` at
      :data:`CORROBORATION_IMPORTANCE` or above;
    * a second day above ruhig inside :data:`REPETITION_DAYS` — ``repeated``,
      which the caller has already asked :func:`_repeated` for, because the rung
      one floor down needs the same answer and one query is enough for both.

    Exactly three, and the list is closed on purpose: the acceptance names these
    and no others, so a fourth route past the brake is a change to the rule and
    not an implementation detail.
    """
    return (
        inputs.outlets >= CORROBORATING_OUTLETS or _flagged(rows) or repeated
    )


def measure(
    session: Session, client: Client, *, now: dt.datetime | None = None
) -> Reading:
    """This mandate's rung right now, counted from stored rows. Writes nothing.

    Four inputs, a sum, a rung, and then the three things the sum alone must not
    decide:

    * **the brake.** A single negative report never lifts a mandate above
      Beobachtung, however large the outlet — see :func:`_corroborated`. This is
      the rule the whole feature stands or falls on: without it the band is red
      every morning and therefore read on none of them.
    * **the second day.** A mandate the arithmetic put on Beobachtung, which
      already stood above ruhig on another day this week, is on
      :attr:`~newspulse.models.ReputationState.ISSUE`: not a bad morning but a
      matter being carried. No sum over one day can reach that rung — a day
      cannot see a second day — so it is read off the stored series. RIS-02's
      register will say *which* matter; the series can already say that there
      is one.
    * **the open crisis.** A declared crisis sets the rung to Krise and the
      arithmetic may not lower it. The tool cannot be showing "ruhig" for a
      mandate whose crisis page is open in the next tab, and the crisis is the
      statement a *person* made — it outranks a count either way.

    A mandate with no coverage in the window scores zero and is ``ruhig``: not
    unknown, and the reading says so by carrying ``articles = 0``.
    """
    reference = now or dt.datetime.now(dt.UTC)
    window = dt.timedelta(hours=config.reputation_window_hours())
    since = reference - window
    rows = _rows(session, client, since=since, until=reference)
    inputs = _inputs(client, rows)
    points = score(inputs)
    computed = _rung(points)

    # Asked once, and only where it can change the answer: a mandate the
    # arithmetic put on ruhig is neither braked nor carrying anything, and this
    # is the one round trip the reading costs beyond reading the coverage.
    repeated = computed is not ReputationState.RUHIG and _repeated(
        session, client, day=_local_day(reference), before=since
    )

    braked = False
    if rank(computed) > rank(ReputationState.BEOBACHTUNG) and not _corroborated(
        inputs, rows, repeated=repeated
    ):
        computed = ReputationState.BEOBACHTUNG
        braked = True
    elif computed is ReputationState.BEOBACHTUNG and repeated:
        # Only from Beobachtung upwards, never from ruhig: a mandate today's
        # coverage says nothing about does not acquire a rung from an older row.
        # A braked reading cannot arrive here at all — the brake fires only when
        # nothing corroborated, and a repetition is one of the three things that
        # does — so the two branches cannot both be right about one reading.
        computed = ReputationState.ISSUE

    if crisis.open_crisis(session, client) is not None:
        computed = ReputationState.KRISE
        # Not a brake: the arithmetic did not reach and was not held back, a
        # person declared. Saying "braked" here would offer the wrong sentence.
        braked = False
    return Reading(inputs=inputs, points=points, state=computed, braked=braked)


# --- Writing the reading ----------------------------------------------------------


def _local_day(moment: dt.datetime) -> dt.date:
    """The local day ``moment`` falls on — the day the Heute page is keyed on."""
    return moment.astimezone(config.local_zone()).date()


def _store(reading: Reading, row: ReputationReading, *, at: dt.datetime) -> None:
    """Write a computed reading onto a row. The one place the columns are set."""
    row.state = reading.state
    row.outlets = reading.inputs.outlets
    row.national = reading.inputs.national
    row.articles = reading.inputs.articles
    row.negative = reading.inputs.negative
    row.named = reading.inputs.named
    row.points = reading.points
    row.computed_at = at


def record(
    session: Session, client: Client, *, now: dt.datetime | None = None
) -> ReputationReading:
    """Take a reading and store it as *the* reading for this mandate and day.

    A second run on the same day updates the standing row rather than adding a
    second one. That is not tidiness: every median and every trend in this module
    is counted over the series, and a day stored twice would weigh double in both
    for as long as it stayed inside their windows.

    The read-then-write is not atomic, so the promise is kept twice — once by the
    read, and once by catching ``uq_reputation_reading_per_day`` doing its job
    when two processes reach the insert together.
    """
    reference = now or dt.datetime.now(dt.UTC)
    day = _local_day(reference)
    reading = measure(session, client, now=reference)

    row = session.scalars(
        select(ReputationReading).where(
            ReputationReading.client_id == client.id, ReputationReading.day == day
        )
    ).first()
    if row is not None:
        _store(reading, row, at=reference)
        session.commit()
        return row

    row = ReputationReading(client_id=client.id, day=day)
    _store(reading, row, at=reference)
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        # The UNIQUE fired: another process wrote this mandate's day between the
        # read above and this commit. Their row is the reading — updated with
        # what this run counted, because both counted the same stored coverage
        # and the later timestamp is the honest one.
        session.rollback()
        standing = session.scalars(
            select(ReputationReading).where(
                ReputationReading.client_id == client.id,
                ReputationReading.day == day,
            )
        ).first()
        if standing is None:
            raise
        _store(reading, standing, at=reference)
        session.commit()
        return standing
    return row


def sweep(
    session: Session, clients: list[Client], *, now: dt.datetime | None = None
) -> int:
    """A reading for every mandate handed in. Returns how many were written.

    One fault boundary per mandate, because a reading is one tile on one line: a
    mandate whose coverage cannot be counted must not cost the other nine their
    readings, and it must not cost the sweep its morning either.

    Yardsticks are skipped. A competitor is tracked to compare its share of the
    conversation; nothing in this tool reports its reputation and the band has no
    tile for it.
    """
    written = 0
    for client in clients:
        if client.is_competitor:
            continue
        # Read before anything is asked: a caught exception may leave the session
        # needing a rollback, and a rollback expires every loaded attribute — so
        # reaching for ``client.name`` inside the handler would be a fresh SELECT
        # on exactly the connection that just failed.
        name = client.name
        try:
            record(session, client, now=now)
        except Exception:  # noqa: BLE001 — a reading is never worth a failed sweep
            session.rollback()
            _log.exception("the reputation reading for %r failed; skipping", name)
            continue
        written += 1
    return written


# --- What a series says -----------------------------------------------------------


def history(
    session: Session, client: Client, *, limit: int, before: dt.date | None = None
) -> list[ReputationReading]:
    """This mandate's ``limit`` most recent readings, newest first.

    ``before`` excludes a day, which is what the baseline needs: a mandate is
    measured against the median of its *previous* readings, and letting today
    into its own baseline would drag the median towards the value it is being
    compared with.
    """
    where = [ReputationReading.client_id == client.id]
    if before is not None:
        where.append(ReputationReading.day < before)
    return list(
        session.scalars(
            select(ReputationReading)
            .where(*where)
            .order_by(ReputationReading.day.desc())
            .limit(limit)
        ).all()
    )


def direction(readings: list[ReputationReading]) -> Direction:
    """Which way these readings are moving, newest first.

    The newest against the median of the ones behind it. A median rather than
    the immediately preceding reading, because a single quiet Sunday between two
    loud weekdays would otherwise read as "steigend" every Monday morning; and
    rather than a mean, because one national wave in the window would pull an
    average up for a week after the coverage stopped.

    Fewer than two readings is :attr:`Direction.STABIL`: there is nothing to
    compare, and inventing a movement out of one point would be the first lie the
    band told.
    """
    if len(readings) < 2:
        return Direction.STABIL
    newest, *earlier = readings[:DIRECTION_READINGS]
    baseline = statistics.median(reading.points for reading in earlier)
    if newest.points > baseline:
        return Direction.STEIGEND
    if newest.points < baseline:
        return Direction.FALLEND
    return Direction.STABIL


def deviates(newest: ReputationReading, baseline: list[ReputationReading]) -> bool:
    """Whether ``newest`` exceeds the median of this mandate's own baseline.

    The point of measuring a mandate against itself: a municipal utility with two
    negative pieces in a month and a listed group with two a week are both normal,
    and one threshold cannot be right for both.

    ``baseline`` must not contain ``newest`` — see :func:`history`. Fewer than
    :data:`BASELINE_MIN_READINGS` readings is not a baseline and answers ``False``:
    with two zeros behind it every third day is "unusual", which would put the
    sentence on every mandate in its first week and empty it of meaning after.
    """
    if len(baseline) < BASELINE_MIN_READINGS:
        return False
    return newest.points > statistics.median(
        reading.points for reading in baseline[:BASELINE_READINGS]
    )


# --- The band ---------------------------------------------------------------------


def _entry(
    session: Session, client: Client, newest: ReputationReading,
    *, state: ReputationState,
) -> BandEntry:
    """One tile, with the two things a single reading cannot say on its own.

    ``state`` is passed rather than read off ``newest`` because the crisis floor
    is applied by the caller — see :func:`_in_crisis`.
    """
    series = history(session, client, limit=DIRECTION_READINGS)
    baseline = history(session, client, limit=BASELINE_READINGS, before=newest.day)
    return BandEntry(
        client_id=client.id,
        client_name=client.name,
        state=state,
        direction=direction(series),
        deviates=deviates(newest, baseline),
        outlets=newest.outlets,
        national=newest.national,
        articles=newest.articles,
        negative=newest.negative,
        named=newest.named,
        day=newest.day,
    )


def _in_crisis(session: Session, clients: list[Client]) -> set[int]:
    """Which of these mandates are in a declared crisis right now.

    The same floor :func:`measure` applies, applied a second time at read.
    Not belt and braces: the reading was taken at 06:10 and a crisis declared at
    two in the afternoon is not in it, so without this the band would sit on the
    same screen as that mandate's own crisis card and call it ruhig until the
    next morning. The floor is the one part of the rung that is a *person's*
    statement rather than a count, and it is true the second it is made.

    Nothing else is recomputed here. The counts on the tile stay the ones the
    sweep stored, so the number a consultant re-derives is still the number that
    was counted.
    """
    ids = [client.id for client in clients]
    if not ids:
        return set()
    return set(
        session.scalars(
            select(Crisis.client_id).where(
                Crisis.client_id.in_(ids), Crisis.closed_at.is_(None)
            )
        ).all()
    )


def _latest(session: Session, clients: list[Client]) -> dict[int, ReputationReading]:
    """Each mandate's most recent reading, by client id.

    One query for the whole portfolio rather than one per mandate: the band is
    rendered on every load of the busiest page in the tool, and ten mandates is
    ten round trips for a line that is usually one sentence long.
    """
    ids = [client.id for client in clients]
    if not ids:
        return {}
    # The newest day per mandate, decided in the database rather than by loading
    # the whole series and keeping the last of each: a year of daily readings is
    # a year of rows to read for a line that is usually one sentence long.
    newest_day = (
        select(
            ReputationReading.client_id.label("client_id"),
            func.max(ReputationReading.day).label("day"),
        )
        .where(ReputationReading.client_id.in_(ids))
        .group_by(ReputationReading.client_id)
        .subquery()
    )
    rows = session.scalars(
        select(ReputationReading).join(
            newest_day,
            (ReputationReading.client_id == newest_day.c.client_id)
            & (ReputationReading.day == newest_day.c.day),
        )
    ).all()
    return {reading.client_id: reading for reading in rows}


def band(session: Session, clients: list[Client]) -> Band:
    """The morning's line, out of stored readings and nothing else.

    Never recomputed on render. The reading is what the sweep counted at 06:10,
    and a band that re-counted on every page load would move under the reader
    during the morning with no run having happened — and would disagree with the
    row a consultant is about to re-derive it from.

    DEC-1 option B: a mandate gets a tile only once it has left the lowest rung,
    and the rest are a count. A mandate with no reading at all is in neither, and
    when *no* mandate has one there is no band — the sweep has never run, and a
    line claiming calm it never measured would be worse than no line.

    The single exception to "never recomputed" is the crisis floor, and it is not
    a recomputation: see :func:`_in_crisis`.
    """
    latest = _latest(session, clients)
    in_crisis = _in_crisis(session, clients)
    entries: list[BandEntry] = []
    quiet = 0
    without_coverage = 0
    for client in clients:
        newest = latest.get(client.id)
        if newest is None:
            continue
        state = newest.state
        if client.id in in_crisis:
            state = ReputationState.KRISE
        if state is ReputationState.RUHIG:
            quiet += 1
            if not newest.articles:
                without_coverage += 1
            continue
        entries.append(_entry(session, client, newest, state=state))
    entries.sort(key=lambda entry: (-rank(entry.state), entry.client_name))
    day = max((reading.day for reading in latest.values()), default=None)
    return Band(
        entries=tuple(entries),
        quiet=quiet,
        without_coverage=without_coverage,
        day=day,
    )


__all__ = [
    "BASELINE_MIN_READINGS",
    "BASELINE_READINGS",
    "CORROBORATING_OUTLETS",
    "CORROBORATION_IMPORTANCE",
    "DIRECTION_READINGS",
    "LADDER",
    "REPETITION_DAYS",
    "Band",
    "BandEntry",
    "Direction",
    "Inputs",
    "Reading",
    "band",
    "deviates",
    "direction",
    "history",
    "measure",
    "rank",
    "record",
    "score",
    "sweep",
]
