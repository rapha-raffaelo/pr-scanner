"""The daily reading: its arithmetic, its brake, and what a series of them says.

Nothing here reaches a model and nothing here reaches the network — DEC-2 locked
"gerechnet aus gespeicherten Zeilen", and that is the claim this file exists to
hold. Every rung is checked against a fixture counted by hand in the docstring
that seeds it, never against the module's own ``score``: a test that asks the
code what the answer is passes on the morning the code is wrong.

The clock is injected rather than patched, everywhere. A reading is a statement
about a *day*, so a test that let ``datetime.now`` decide would pass at 14:00
and fail at 00:30 for reasons that have nothing to do with the arithmetic.

The brake gets a file section of its own. It is the most expensive thing here to
get wrong: a band that lifts a mandate off the lowest rung for one negative
piece is red every morning, and a band that is red every morning is read on
none of them.
"""

from __future__ import annotations

import datetime as dt
import hashlib

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import config, reputation
from newspulse.matching import title_hash
from newspulse.models import (
    Analysis,
    Article,
    Base,
    Category,
    Client,
    Crisis,
    ReputationReading,
    ReputationState,
    Tonality,
)

#: Fixed, and inside the local day it is meant to be inside: the reading is
#: keyed on the *local* day, so a reference hour near midnight UTC would file
#: the seeded coverage on either side of the boundary depending on the runner's
#: zone. Ten in the morning Berlin time is nowhere near either edge.
_NOW = dt.datetime(2026, 9, 2, 8, 0, tzinfo=dt.UTC)

#: Each outlet's own wording of the same event, one filler word apart — the same
#: fixture shape ``test_crisis`` uses, and for the same reason: real syndication
#: is not copy-paste, and identical headlines would be collapsed by dedup rather
#: than grouped by the clusterer this module counts outlets through.
#:
#: Five significant tokens each after the stopwords come out; any two share four
#: of six, which is 0.67 against the clusterer's 0.6 bar.
_WORDINGS = {
    "FAZ": "offiziell",
    "Handelsblatt": "formell",
    "Badische Zeitung": "erneut",
    "Nordkurier": "scharf",
    "PV Magazine": "abermals",
    "Solarserver": "schriftlich",
}

#: A second, unrelated event. Deliberately shares no significant token with
#: :func:`_wording`, so the clusterer files it as its own story — which is what
#: makes "two outlets on two different things" expressible at all.
_OTHER = "Solaris AG streicht Sponsoring des Stadtfestes zusammen"

#: A story about the mandate's field that never names it. Used where "namentlich
#: genannt" has to be false.
_MARKET = "Photovoltaik Ausbau verliert bundesweit deutlich an Tempo"


def _wording(source: str) -> str:
    """This outlet's headline for the seeded story."""
    return (
        f"Verbraucherzentrale mahnt Solaris AG {_WORDINGS[source]} "
        "wegen Werbeversprechen ab"
    )


@pytest.fixture
def factory():
    """A session factory over one in-memory database, shared by every session."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def session(factory):
    with factory() as open_session:
        yield open_session


@pytest.fixture
def mandate(session) -> Client:
    client = Client(name="Solaris AG", aliases=["Solaris"], industry="Solarenergie")
    session.add(client)
    session.commit()
    return client


# --- Builders ---------------------------------------------------------------------


def _slug(title: str, source: str) -> str:
    """A short, stable path segment for a fixture article."""
    return hashlib.sha1(f"{title}|{source}".encode()).hexdigest()[:12]


def _cover(
    session,
    client: Client,
    *,
    source: str,
    title: str | None = None,
    tonality: Tonality = Tonality.NEGATIV,
    category: Category = Category.SONSTIGES,
    importance: int = 6,
    published: dt.datetime | None = None,
) -> Article:
    """One stored article plus this mandate's analysis of it."""
    at = published or _NOW - dt.timedelta(hours=2)
    title = title or _wording(source)
    article = Article(
        title=title,
        url=f"https://{source.replace(' ', '').lower()}.example.de/{_slug(title, source)}",
        source=source,
        published_at=at,
        fetched_at=at,
        summary_text="Eine kurze Zusammenfassung.",
        title_hash=title_hash(title, source),
    )
    session.add(article)
    session.flush()
    session.add(
        Analysis(
            article_id=article.id,
            client_id=client.id,
            is_relevant=True,
            summary="Zusammenfassung.",
            category=category,
            relevance_score=7,
            importance_score=importance,
            tonality=tonality,
            analyzed_at=at,
        )
    )
    session.commit()
    return article


def _reading_on(
    session, client: Client, day: dt.date, *, negative: int = 1, points: int = 2,
    state: ReputationState = ReputationState.BEOBACHTUNG, articles: int | None = None,
) -> ReputationReading:
    """A stored reading for a past day, seeded rather than computed.

    Seeded on purpose: the questions these rows answer — the direction, the
    baseline, the brake's repetition — are about a *series*, and building the
    series out of coverage would test the arithmetic a second time instead of
    testing what reads the series.
    """
    row = ReputationReading(
        client_id=client.id,
        day=day,
        state=state,
        outlets=2 if negative else 0,
        national=False,
        articles=negative if articles is None else articles,
        negative=negative,
        named=bool(negative),
        points=points,
        computed_at=dt.datetime.combine(day, dt.time(6, 10), tzinfo=dt.UTC),
    )
    session.add(row)
    session.commit()
    return row


def _local_today() -> dt.date:
    return _NOW.astimezone(config.local_zone()).date()


# --- The five rungs, counted against fixtures counted by hand ---------------------


def test_no_coverage_at_all_reads_quiet_and_says_nothing_lay(session, mandate):
    """A mandate nobody wrote about is ruhig, not unknown.

    Nothing in the window: zero outlets, zero articles, no national reach, no
    name. Zero points, and the bottom rung. The row is what says so — it carries
    ``articles = 0`` rather than being absent, which is the difference between
    "nothing was written" and "nobody looked".
    """
    reading = reputation.measure(session, mandate, now=_NOW)

    assert reading.inputs.articles == 0
    assert reading.inputs.negative == 0
    assert reading.points == 0
    assert reading.state is ReputationState.RUHIG


def test_coverage_that_is_not_negative_reads_quiet(session, mandate):
    """Three friendly pieces, one of them national.

    Counted by hand: the negative set is empty, so the widest negative story has
    zero outlets, nothing ran nationally *against* the mandate, the share is
    0/3, and nothing negative names it. Zero points, ruhig — the reading is about
    what the coverage does to the mandate, not about how much of it there is.
    """
    for source in ("FAZ", "Handelsblatt", "PV Magazine"):
        _cover(session, mandate, source=source, tonality=Tonality.POSITIV)

    reading = reputation.measure(session, mandate, now=_NOW)

    assert reading.inputs.articles == 3
    assert reading.inputs.negative == 0
    assert reading.points == 0
    assert reading.state is ReputationState.RUHIG


def test_two_regional_outlets_on_one_story_read_as_beobachtung(session, mandate):
    """Two regional outlets, both negative, the mandate named.

    Counted by hand:

    * the widest negative story carries two outlets -> 1 point (bucket "≥2")
    * neither Badische Zeitung nor Nordkurier is tier 1 -> 0
    * two of two read negative, a share of 1.0 -> 2 points
    * both headlines name "Solaris AG" -> 1 point

    Four points, which is the "at least four" band: Risiko. And it stays Risiko
    — two independent outlets is the brake's first corroboration, so nothing
    holds it down.
    """
    for source in ("Badische Zeitung", "Nordkurier"):
        _cover(session, mandate, source=source)

    reading = reputation.measure(session, mandate, now=_NOW)

    assert reading.inputs.outlets == 2
    assert reading.inputs.national is False
    assert (reading.inputs.negative, reading.inputs.articles) == (2, 2)
    assert reading.inputs.named is True
    assert reading.points == 4
    assert reading.state is ReputationState.RISIKO
    assert reading.braked is False


def test_the_top_rung_is_every_input_at_its_maximum(session, mandate):
    """Five outlets on one story, one of them national, all negative, named.

    Counted by hand: 3 + 2 + 2 + 1 = 8 points, which is the full house. Seven is
    where the top rung starts, so eight is above it and nothing less than seven
    reaches it.
    """
    for source in ("FAZ", "Badische Zeitung", "Nordkurier", "PV Magazine", "Solarserver"):
        _cover(session, mandate, source=source)

    reading = reputation.measure(session, mandate, now=_NOW)

    assert reading.inputs.outlets == 5
    assert reading.inputs.national is True
    assert reading.inputs.negative_share == pytest.approx(1.0)
    assert reading.points == 8
    assert reading.state is ReputationState.KRISE


def test_a_contested_story_the_mandate_is_not_named_in_reads_lower(session, mandate):
    """Three outlets on one story, half of the day's coverage negative, no name.

    Counted by hand:

    * three outlets on the widest negative story -> 2 points (bucket "≥3")
    * none of the three is tier 1 -> 0
    * three of six read negative, a share of 0.5 -> 1 point
    * the market headline never says "Solaris" -> 0

    Three points: Beobachtung. The same three outlets *with* the mandate named
    would be four and Risiko, which is the one-point difference between "the
    field is under pressure" and "we are".
    """
    for source in ("Badische Zeitung", "Nordkurier", "PV Magazine"):
        _cover(session, mandate, source=source, title=f"{_MARKET} {source[:4]}")
    for source in ("Solarserver", "Handelsblatt", "FAZ"):
        _cover(session, mandate, source=source, tonality=Tonality.NEUTRAL,
               title=f"Solaris AG eroeffnet Werk in {source[:4]}land")

    reading = reputation.measure(session, mandate, now=_NOW)

    assert reading.inputs.outlets == 3
    assert reading.inputs.named is False
    assert (reading.inputs.negative, reading.inputs.articles) == (3, 6)
    assert reading.points == 3
    assert reading.state is ReputationState.BEOBACHTUNG


def test_no_sum_of_points_reaches_the_issue_rung(session, mandate):
    """Issue is the series' answer and never the sum's, by construction.

    Every reachable sum is walked rather than a sample of them: the rung is
    decided by a table, and a table is exactly the thing that gets an entry added
    to it by somebody who has not read this. What a day *cannot* see is a second
    day, so that is the one thing the rung is reached by — see
    ``test_a_beobachtung_that_was_already_there_yesterday_is_an_issue``.
    """
    reached = {reputation._rung(points) for points in range(0, 9)}

    assert ReputationState.ISSUE not in reached
    assert reached == {
        ReputationState.RUHIG,
        ReputationState.BEOBACHTUNG,
        ReputationState.RISIKO,
        ReputationState.KRISE,
    }


def test_coverage_outside_the_window_is_not_counted(session, mandate):
    """The reading is about a day, and yesterday's wave is not today's.

    Two outlets today and three the day before last. Only the two are inside the
    24-hour window, so the day before last cannot keep the band red after the
    press has moved on — which is the whole reason the window is a day.
    """
    for source in ("Badische Zeitung", "Nordkurier"):
        _cover(session, mandate, source=source)
    for source in ("FAZ", "Handelsblatt", "PV Magazine"):
        _cover(session, mandate, source=source,
               published=_NOW - dt.timedelta(days=2))

    reading = reputation.measure(session, mandate, now=_NOW)

    assert reading.inputs.articles == 2
    assert reading.inputs.outlets == 2


def test_a_dismissed_analysis_leaves_the_count(session, mandate):
    """"Nicht relevant" is the one click that says the matcher was wrong.

    A row a person removed from the mandate's coverage may not go on driving its
    rung, or the click would be visibly ignored on the page above the coverage
    it just left.
    """
    for source in ("Badische Zeitung", "Nordkurier"):
        article = _cover(session, mandate, source=source)
    analysis = session.scalars(
        select(Analysis).where(Analysis.article_id == article.id)
    ).one()
    analysis.dismissed_at = _NOW
    session.commit()

    reading = reputation.measure(session, mandate, now=_NOW)

    assert reading.inputs.articles == 1
    assert reading.inputs.outlets == 1


# --- The brake: a single negative report never lifts past Beobachtung -------------


def test_one_national_negative_report_stays_on_beobachtung(session, mandate):
    """The rule the whole feature stands on, and the one it is easiest to lose.

    One piece, in the largest outlet there is, wholly negative, naming the
    mandate. The arithmetic reaches four points — 0 outlets + 2 national + 2
    share + 0... no: one outlet scores nothing, national is 2, the share is 1.0
    for 2, and the name is 1. Five points, which is Risiko.

    It stays on Beobachtung, because one outlet is one outlet's angle however
    loudly it is published. The unbraked answer is asserted alongside so this
    test fails if the brake starts being achieved by quietly lowering the sum
    instead — the sum is what a consultant re-derives, and it has to keep saying
    what the coverage was.
    """
    _cover(session, mandate, source="FAZ")

    reading = reputation.measure(session, mandate, now=_NOW)

    assert reading.inputs.outlets == 1
    assert reading.inputs.national is True
    assert reading.points == 5
    assert reading.state is ReputationState.BEOBACHTUNG
    assert reading.braked is True


def test_two_outlets_on_two_different_stories_do_not_corroborate(session, mandate):
    """"Zwei unabhängige Medien" means two on the same thing.

    Two national outlets, each with its own unrelated story about the mandate.
    Neither story has a second outlet, so the widest negative story is one outlet
    wide and the brake holds — a mandate that had two separate bad afternoons is
    not a mandate two newsrooms are chasing.
    """
    _cover(session, mandate, source="FAZ")
    _cover(session, mandate, source="Handelsblatt", title=_OTHER)

    reading = reputation.measure(session, mandate, now=_NOW)

    assert reading.inputs.outlets == 1
    assert reading.inputs.articles == 2
    assert reading.state is ReputationState.BEOBACHTUNG
    assert reading.braked is True


def test_a_second_outlet_on_the_same_story_lifts_it(session, mandate):
    """The brake's first corroboration, against the test above it.

    The same national piece, plus one regional outlet on the *same* story. Two
    independent outlets, so the brake lets go and the arithmetic stands.
    """
    _cover(session, mandate, source="FAZ")
    _cover(session, mandate, source="Nordkurier")

    reading = reputation.measure(session, mandate, now=_NOW)

    assert reading.inputs.outlets == 2
    assert reading.state is ReputationState.RISIKO
    assert reading.braked is False


def test_a_krise_analysis_at_importance_eight_lifts_a_single_report(session, mandate):
    """The brake's second corroboration.

    One outlet, but the analyzer both filed it as ``krise`` and rated it eight of
    ten. That is not a bad news day, and it is the one case where a single piece
    says enough on its own.
    """
    _cover(
        session, mandate, source="FAZ",
        category=Category.KRISE,
        importance=reputation.CORROBORATION_IMPORTANCE,
    )

    reading = reputation.measure(session, mandate, now=_NOW)

    assert reading.inputs.outlets == 1
    assert reading.state is ReputationState.RISIKO
    assert reading.braked is False


def test_a_krise_analysis_below_the_importance_does_not_lift_it(session, mandate):
    """One below the bar is still one below the bar.

    The threshold is the whole content of the condition: without it, every
    ``krise`` category — which the analyzer files freely at five and six — would
    corroborate, and the brake would be off for the coverage it matters most on.
    """
    _cover(
        session, mandate, source="FAZ",
        category=Category.KRISE,
        importance=reputation.CORROBORATION_IMPORTANCE - 1,
    )

    reading = reputation.measure(session, mandate, now=_NOW)

    assert reading.state is ReputationState.BEOBACHTUNG
    assert reading.braked is True


def test_a_positive_krise_analysis_does_not_corroborate(session, mandate):
    """The second corroboration is about the report the brake is holding.

    One hostile FAZ piece, and beside it a friendly one the analyzer filed under
    ``krise`` at eight — "die Krise gemeistert" is a crisis story and reads
    positiv. Counted over every row, that lifts the mandate to Risiko off one
    negative report, one outlet and no second day, which is the exact false
    alarm the brake exists to refuse. Counted over the negative rows, as every
    other input on the reading is, nothing corroborates and the brake holds.
    """
    _cover(session, mandate, source="FAZ")
    _cover(
        session, mandate, source="Handelsblatt", title=_MARKET,
        tonality=Tonality.POSITIV, category=Category.KRISE,
        importance=reputation.CORROBORATION_IMPORTANCE,
    )

    reading = reputation.measure(session, mandate, now=_NOW)

    assert reading.inputs.outlets == 1
    assert reading.state is ReputationState.BEOBACHTUNG
    assert reading.braked is True


def test_one_report_cannot_be_its_own_repetition(session, mandate):
    """Two readings whose windows overlap have seen the same article.

    The window is a rolling stretch of hours ending at the reading's moment, so
    a run at 20:00 and a run at 06:00 the next morning both count a story filed
    the previous morning — and it is filed under two different local days. That
    is the dashboard's "jetzt lesen" followed by the scheduled run, and without
    the disjointness guard the second reading reads the first as "it was there
    yesterday too" and reaches Risiko off one report in one outlet.
    """
    story = _NOW - dt.timedelta(days=1, hours=2)
    _cover(session, mandate, source="FAZ", published=story)

    first = reputation.record(session, mandate, now=story + dt.timedelta(hours=12))
    second = reputation.record(session, mandate, now=story + dt.timedelta(hours=22))

    # Two days, one article: the windows overlap and both saw it.
    assert first.day != second.day
    assert second.negative == 1
    assert second.state is ReputationState.BEOBACHTUNG


def test_a_day_whose_rung_came_only_from_the_crisis_floor_is_not_a_repetition(
    session, mandate
):
    """A person's declaration may not do duty as media corroboration.

    Monday's row reads ``krise`` and carries zero points: the arithmetic put the
    mandate on ruhig and an open crisis floored it there, which is a statement a
    person made rather than a count of coverage. Today has one hostile report.
    Reading Monday's *state* would call that a repetition and lift today to
    Risiko; reading its ``points`` asks what the arithmetic said, and the
    arithmetic said nothing.
    """
    _reading_on(
        session, mandate, _local_today() - dt.timedelta(days=2),
        negative=1, points=0, state=ReputationState.KRISE,
    )
    _cover(session, mandate, source="FAZ")

    reading = reputation.measure(session, mandate, now=_NOW)

    assert reading.state is ReputationState.BEOBACHTUNG
    assert reading.braked is True


def test_a_repetition_on_a_second_day_lifts_a_single_report(session, mandate):
    """The brake's third corroboration, read off yesterday's stored reading.

    One outlet today, and a reading from yesterday that already saw negative
    coverage. Twice in two days is the difference between an incident and a
    thing that is going on — which is the whole reason the reading is stored
    rather than recomputed.
    """
    _reading_on(session, mandate, _local_today() - dt.timedelta(days=1))
    _cover(session, mandate, source="FAZ")

    reading = reputation.measure(session, mandate, now=_NOW)

    assert reading.state is ReputationState.RISIKO
    assert reading.braked is False


def test_a_quiet_yesterday_does_not_corroborate(session, mandate):
    """A stored reading is not itself corroboration — a *negative* one is.

    Yesterday's row exists and saw nothing. The brake holds, because the series
    says the opposite of a repetition.
    """
    _reading_on(
        session, mandate, _local_today() - dt.timedelta(days=1),
        negative=0, points=0, state=ReputationState.RUHIG, articles=0,
    )
    _cover(session, mandate, source="FAZ")

    reading = reputation.measure(session, mandate, now=_NOW)

    assert reading.state is ReputationState.BEOBACHTUNG
    assert reading.braked is True


def test_a_negative_day_beyond_the_repetition_window_does_not_corroborate(
    session, mandate
):
    """Two bad days two months apart are two days, not a repetition."""
    _reading_on(
        session, mandate,
        _local_today() - dt.timedelta(days=reputation.REPETITION_DAYS + 1),
    )
    _cover(session, mandate, source="FAZ")

    reading = reputation.measure(session, mandate, now=_NOW)

    assert reading.state is ReputationState.BEOBACHTUNG


def test_a_second_run_the_same_day_cannot_corroborate_off_its_own_row(
    session, mandate
):
    """The brake must not switch itself off on the second run of a morning.

    The first run stores a reading that saw negative coverage. If the repetition
    check looked at today as well as at the days before it, the second run would
    read its own row back as "it happened yesterday too" — and every mandate in
    the portfolio would clear the brake on any day the sweep ran twice.
    """
    _cover(session, mandate, source="FAZ")
    first = reputation.record(session, mandate, now=_NOW)
    assert first.state is ReputationState.BEOBACHTUNG

    again = reputation.measure(session, mandate, now=_NOW + dt.timedelta(hours=2))

    assert again.state is ReputationState.BEOBACHTUNG
    assert again.braked is True


# --- The fifth rung: a matter that was already there on a second day -------------


def test_a_beobachtung_that_was_already_there_yesterday_is_an_issue(session, mandate):
    """The rung between a bad morning and a risk, and the one a day cannot see.

    Counted by hand:

    * one outlet on the negative story -> 0 points (below the "≥2" bucket)
    * Badische Zeitung is not tier 1 -> 0
    * one of one reads negative, a share of 1.0 -> 2 points
    * the headline names "Solaris AG" -> 1 point

    Three points: Beobachtung, exactly as it would be on its own. The sum is what
    the coverage was and does not move. What moves the rung is yesterday's stored
    reading, which already stood above ruhig: the same matter on a second day is
    a matter being carried, and that is Issue.
    """
    _reading_on(session, mandate, _local_today() - dt.timedelta(days=1))
    _cover(session, mandate, source="Badische Zeitung")

    reading = reputation.measure(session, mandate, now=_NOW)

    assert reading.points == 3
    assert reading.state is ReputationState.ISSUE
    # Nothing was held down: the brake is about a rung the sum reached and could
    # not keep, and this is a rung the sum never reached at all.
    assert reading.braked is False


def test_the_same_coverage_on_its_own_first_day_is_only_beobachtung(session, mandate):
    """The pair to the test above, and the reason it is about a *second* day.

    The identical fixture with no history behind it. Three points either way, so
    a mandate does not reach Issue by having a bad Tuesday — it reaches it by
    still being there on Wednesday.
    """
    _cover(session, mandate, source="Badische Zeitung")

    reading = reputation.measure(session, mandate, now=_NOW)

    assert reading.points == 3
    assert reading.state is ReputationState.BEOBACHTUNG


def test_an_earlier_day_the_reading_itself_called_ruhig_is_not_a_repetition(
    session, mandate
):
    """A repetition is a repetition *of something*, so the earlier day has to be one.

    Yesterday's row saw a negative piece and its own arithmetic still put the
    mandate on ruhig — one hostile article inside twenty friendly ones is not a
    matter. Counting that as the second day would let two unrelated small pieces
    a week apart clear the brake, which is the false alarm the brake exists to
    refuse. The stored ``state`` is that day's own answer to exactly this
    question, so it is the one asked.
    """
    _reading_on(
        session, mandate, _local_today() - dt.timedelta(days=1),
        negative=1, articles=20, points=0, state=ReputationState.RUHIG,
    )
    _cover(session, mandate, source="FAZ")

    reading = reputation.measure(session, mandate, now=_NOW)

    assert reading.state is ReputationState.BEOBACHTUNG
    assert reading.braked is True


def test_a_mandate_with_nothing_today_stays_ruhig_however_loud_yesterday_was(
    session, mandate
):
    """Issue is reached from Beobachtung upwards and never from ruhig.

    Yesterday was a Risiko and today nobody wrote about the mandate at all. The
    reading is about a day: a mandate today's coverage says nothing about does
    not acquire a rung from an older row, and the quiet line on the band counts
    it — which is the difference between a band that empties out when a story
    passes and one that stays red for a week after it has.
    """
    _reading_on(
        session, mandate, _local_today() - dt.timedelta(days=1),
        points=4, state=ReputationState.RISIKO,
    )

    reading = reputation.measure(session, mandate, now=_NOW)

    assert reading.inputs.articles == 0
    assert reading.state is ReputationState.RUHIG


# --- The open crisis is a floor the arithmetic cannot lower ----------------------


def test_an_open_crisis_holds_the_rung_at_krise_with_no_coverage_at_all(
    session, mandate
):
    """The arithmetic says ruhig; a person says crisis, and the person wins.

    The window is empty — no coverage, zero points — which on its own is the
    bottom rung. The tool may not be showing "ruhig" for a mandate whose crisis
    page is open in the next tab, and a declared crisis is a statement a person
    made rather than a count anything can outvote.
    """
    trigger = _cover(session, mandate, source="FAZ",
                     published=_NOW - dt.timedelta(days=5))
    session.add(
        Crisis(
            client_id=mandate.id,
            article_id=trigger.id,
            declared_by="lucas",
            declared_at=_NOW - dt.timedelta(days=4),
            level=3,
        )
    )
    session.commit()

    reading = reputation.measure(session, mandate, now=_NOW)

    assert reading.points == 0
    assert reading.inputs.articles == 0
    assert reading.state is ReputationState.KRISE
    # Not "braked": nothing was held down, the floor was put in by a person.
    assert reading.braked is False


def test_a_closed_crisis_no_longer_holds_the_rung(session, mandate):
    """A stood-down crisis is a finished document, not a standing state."""
    trigger = _cover(session, mandate, source="FAZ",
                     published=_NOW - dt.timedelta(days=5))
    session.add(
        Crisis(
            client_id=mandate.id,
            article_id=trigger.id,
            declared_by="lucas",
            declared_at=_NOW - dt.timedelta(days=4),
            closed_at=_NOW - dt.timedelta(days=1),
            close_reason="vorbei",
            level=3,
        )
    )
    session.commit()

    reading = reputation.measure(session, mandate, now=_NOW)

    assert reading.state is ReputationState.RUHIG


# --- One reading per mandate and day ---------------------------------------------


def test_the_reading_stores_the_rung_the_four_inputs_and_the_moment(session, mandate):
    for source in ("Badische Zeitung", "Nordkurier"):
        _cover(session, mandate, source=source)

    row = reputation.record(session, mandate, now=_NOW)

    assert row.day == _local_today()
    assert row.state is ReputationState.RISIKO
    assert (row.outlets, row.national, row.articles, row.negative, row.named) == (
        2, False, 2, 2, True,
    )
    assert row.points == 4
    assert row.computed_at == _NOW


def test_a_second_run_the_same_day_updates_the_reading_rather_than_adding_one(
    session, mandate, factory
):
    """One row per mandate and day, and the second run is an update.

    Not tidiness. Every median and every trend in this module is counted over the
    series, so a day stored twice would weigh double in both for as long as it
    stayed inside their windows — and the mandate would then look unusual to
    itself for a fortnight because a redeploy ran the sweep at lunchtime.

    Read back through a second session, because the point is what is in the
    table and not what an object in this test is still holding.
    """
    _cover(session, mandate, source="FAZ")
    reputation.record(session, mandate, now=_NOW)

    for source in ("Badische Zeitung", "Nordkurier"):
        _cover(session, mandate, source=source)
    later = _NOW + dt.timedelta(hours=8)
    reputation.record(session, mandate, now=later)

    with factory() as other:
        assert other.scalar(
            select(func.count()).select_from(ReputationReading)
        ) == 1
        row = other.scalars(select(ReputationReading)).one()
        # The updated row is the second run's count, not the first's: three
        # outlets on one story (2) + national (2) + a share of 3/3 (2) + named
        # (1) = 7, which is the top rung.
        assert row.state is ReputationState.KRISE
        assert row.points == 7
        assert row.outlets == 3
        assert row.computed_at == later


def test_the_sweep_writes_one_reading_per_mandate_and_skips_yardsticks(session):
    """A competitor is tracked to compare its share of the conversation.

    Nothing in the tool reports its reputation and the band has no tile for it,
    so it gets no reading — a row nobody would ever read, counted into a
    portfolio's "ruhig" line, would make the count wrong on the one line that has
    to be right.
    """
    mandates = [Client(name="Solaris AG"), Client(name="Helios GmbH")]
    rival = Client(name="Rivale AG", is_competitor=True)
    session.add_all([*mandates, rival])
    session.commit()

    written = reputation.sweep(session, [*mandates, rival], now=_NOW)

    assert written == 2
    stored = session.scalars(select(ReputationReading)).all()
    assert {row.client_id for row in stored} == {m.id for m in mandates}


def test_one_mandate_that_cannot_be_read_does_not_cost_the_others_theirs(
    session, monkeypatch
):
    """A reading is one tile on one line, and never worth a failed sweep."""
    first, second = Client(name="Solaris AG"), Client(name="Helios GmbH")
    session.add_all([first, second])
    session.commit()
    real = reputation.record

    def _explode(current_session, client, **kwargs):
        if client.id == first.id:
            raise RuntimeError("the coverage could not be counted")
        return real(current_session, client, **kwargs)

    monkeypatch.setattr(reputation, "record", _explode)

    written = reputation.sweep(session, [first, second], now=_NOW)

    assert written == 1
    assert session.scalars(select(ReputationReading)).one().client_id == second.id


# --- What a series says: the direction and the mandate's own baseline ------------


def _series(session, mandate, points: list[int]) -> list[ReputationReading]:
    """Readings for consecutive days, ``points`` given oldest first."""
    start = _local_today() - dt.timedelta(days=len(points) - 1)
    for offset, value in enumerate(points):
        _reading_on(
            session, mandate, start + dt.timedelta(days=offset),
            points=value, negative=1 if value else 0,
        )
    return reputation.history(session, mandate, limit=reputation.DIRECTION_READINGS)


@pytest.mark.parametrize(
    "points,expected",
    [
        # Newest is 5, the median of (0, 0, 1, 1) behind it is 0.5 -> up.
        ([0, 0, 1, 1, 5], reputation.Direction.STEIGEND),
        # Newest is 0, the median of (4, 4, 5, 5) is 4.5 -> down.
        ([4, 4, 5, 5, 0], reputation.Direction.FALLEND),
        # Newest is 3 and so is the median of (3, 3, 3, 3) -> flat.
        ([3, 3, 3, 3, 3], reputation.Direction.STABIL),
        # A single quiet Sunday between loud weekdays: the newest 4 against the
        # median of (4, 4, 0, 4), which is 4. Flat, and that is the reason the
        # comparison is against a median rather than against yesterday alone.
        ([4, 4, 0, 4, 4], reputation.Direction.STABIL),
    ],
)
def test_the_direction_is_the_newest_reading_against_the_median_behind_it(
    session, mandate, points, expected
):
    assert reputation.direction(_series(session, mandate, points)) is expected


def test_a_single_reading_has_no_direction_rather_than_an_invented_one(
    session, mandate
):
    """One point is not a movement, and saying "stabil" is the honest answer."""
    assert reputation.direction(_series(session, mandate, [4])) is (
        reputation.Direction.STABIL
    )


def test_the_direction_reads_only_the_last_seven(session, mandate):
    """A fortnight of calm before a loud week must not flatten the week.

    Ten days: three quiet, then seven at four points each. Reading all ten would
    compare 4 against a median dragged down by the three, and report a rise that
    finished three days ago.
    """
    series = _series(session, mandate, [0, 0, 0, 4, 4, 4, 4, 4, 4, 4])

    assert len(series) == reputation.DIRECTION_READINGS
    assert reputation.direction(series) is reputation.Direction.STABIL


def test_a_mandate_is_unusual_when_it_exceeds_its_own_median(session, mandate):
    """Measured against itself, which is the whole point of storing a series.

    Ten prior readings at one point each — an ordinary month for a mandate that
    is written about a little — and a newest at four. Four is above a median of
    one, so today is named as a change for *this* mandate. The same four would be
    unremarkable for a mandate whose median is four, and no single threshold can
    say both.
    """
    _series(session, mandate, [1] * 10 + [4])
    newest, *_ = reputation.history(session, mandate, limit=1)
    baseline = reputation.history(
        session, mandate, limit=reputation.BASELINE_READINGS, before=newest.day
    )

    assert reputation.deviates(newest, baseline) is True


def test_a_reading_at_its_own_median_is_not_a_change(session, mandate):
    """Strictly above, not at: a mandate that is always at four is not unusual
    on the day it is at four again."""
    _series(session, mandate, [4] * 11)
    newest, *_ = reputation.history(session, mandate, limit=1)
    baseline = reputation.history(
        session, mandate, limit=reputation.BASELINE_READINGS, before=newest.day
    )

    assert reputation.deviates(newest, baseline) is False


def test_a_mandate_without_enough_history_claims_no_baseline(session, mandate):
    """Two readings are not a baseline.

    With one zero behind it every second day would be "unusual for this mandate",
    the band would carry the sentence for every mandate in its first week, and it
    would mean nothing by it ever after.
    """
    _series(session, mandate, [0, 4])
    newest, *_ = reputation.history(session, mandate, limit=1)
    baseline = reputation.history(
        session, mandate, limit=reputation.BASELINE_READINGS, before=newest.day
    )

    assert len(baseline) < reputation.BASELINE_MIN_READINGS
    assert reputation.deviates(newest, baseline) is False
