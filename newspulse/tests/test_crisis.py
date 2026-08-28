"""The crisis as an object, its computed level, and the tighter cadence.

Nothing here reaches a model and nothing here reaches the network. That is not
merely a house rule in this file — it is the story's central claim. A crisis
level a model estimates is a number nobody can re-derive, produced in exactly the
hour somebody wants to re-derive it, so the level is counted from stored rows and
every count is checked here against a fixture computed by hand in the docstring
that seeds it.

Two other properties get the same attention, because both are the kind that pass
every test until the morning they matter:

* a *proposal* writes nothing at all. DEC-1 locked "the tool proposes, a person
  declares", and a proposal that quietly changed the cadence would be option B
  wearing option A's label;
* the cadence's state lives in the ``crises`` row. A crash halfway through a
  crisis reading must leave neither a hung crisis nor a second reading racing the
  first, and the only way to show that is to kill one and read the table back
  through a *different* session.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import config, crisis, gnews, job
from newspulse.ingest import FeedItem
from newspulse.matching import title_hash
from newspulse.models import (
    Analysis,
    Angle,
    Article,
    Base,
    Category,
    Client,
    Crisis,
    Run,
    Tonality,
)
from newspulse.schemas import Analysis as AnalysisSchema

_NOW = dt.datetime(2026, 8, 28, 9, 0, tzinfo=dt.UTC)

#: Each outlet's own wording of the same event, one filler word apart.
#:
#: Deliberately not one headline repeated. Real syndication is not copy-paste —
#: the wire lands and every desk rewrites the top line — and the two questions
#: this codebase asks about that are different ones: dedup collapses an
#: *identical* normalized headline (the same article fetched twice) while
#: ``stories.cluster`` groups the same *event*. Seeding identical copies would
#: quietly test the level against a story the real pipeline would have collapsed
#: to a single article.
#:
#: Five significant tokens each after the stopwords, the two-letter words and the
#: outlet name come out; any two share four of six, which is 0.67 against the
#: clusterer's 0.6 bar.
_WORDINGS = {
    "FAZ": "offiziell",
    "Handelsblatt": "formell",
    "Badische Zeitung": "erneut",
    "Nordkurier": "scharf",
    "PV Magazine": "abermals",
    "Solarserver": "schriftlich",
    "Merkur": "umgehend",
    "Wochenblatt": "nunmehr",
    "Kreiszeitung": "vorsorglich",
    "Rheinpfalz": "vorlaeufig",
    "Nordbayern": "zusaetzlich",
}


def _wording(source: str) -> str:
    """This outlet's headline for the seeded story."""
    return (
        f"Verbraucherzentrale mahnt Solaris AG {_WORDINGS[source]} "
        "wegen Werbeversprechen ab"
    )


#: A story about the mandate's field that never names it. Used where "namentlich
#: genannt" has to be false.
_MARKET_HEADLINE = "Photovoltaik Ausbau verliert bundesweit deutlich an Tempo"


@pytest.fixture
def factory():
    """A session factory over one in-memory database, shared by every session.

    A factory rather than a single session, because two tests here have to read
    the same rows through a *second* session to show that the state is in the
    table and not in an object somebody is still holding.
    """
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


# --- Builders -------------------------------------------------------------------


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
        url=f"https://{source.replace(' ', '').lower()}.example.de/{abs(hash((title, source))) % 10**8}",
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


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


class _FakeAnalyzer:
    """One analysis per article, with the tonality the test asked for."""

    def __init__(self, *, tonality: Tonality = Tonality.NEGATIV) -> None:
        self.calls: list[tuple[str, int]] = []
        self._tonality = tonality

    def analyze(self, client, articles):
        self.calls.append((client.name, len(articles)))
        return [
            AnalysisSchema(
                article_id=article.id,
                client_id=client.id,
                is_relevant=True,
                summary=f"{client.name}: {article.title}",
                category=Category.KRISE,
                relevance_score=8,
                importance_score=8,
                is_alert=True,
                tonality=self._tonality,
                reasoning="fake",
            )
            for article in articles
        ]


def _fetch_recording(mapping: dict[str, list[FeedItem]], seen: list[str]):
    """A ``fetch`` that records every URL it was asked for."""

    def _fetch(url, since, *, source=None, fetched_at=None, **_):
        seen.append(url)
        return list(mapping.get(url, []))

    return _fetch


# --- The level: counted, and counted against a hand-computed fixture ------------


def test_the_level_matches_a_fixture_counted_by_hand(session, mandate):
    """Five outlets, one of them national, four of five negative, mandate named.

    Counted by hand, and the arithmetic is the whole point of the feature:

    * five distinct outlets  -> 2 points (the bucket is "at least five")
    * FAZ and Handelsblatt are tier 1, so the reach is national -> 2 points
    * four of the five read negative, a share of 0.8 -> 2 points
    * the headline names "Solaris AG" -> 1 point

    Seven points, which lands in the "at least six" band: level 4. Not five —
    level 5 is every input at its maximum and nothing less.
    """
    for source in ("FAZ", "Handelsblatt", "Badische Zeitung", "Nordkurier"):
        trigger = _cover(session, mandate, source=source)
    _cover(session, mandate, source="PV Magazine", tonality=Tonality.NEUTRAL)

    graded = crisis.severity(session, mandate, trigger)

    assert graded.outlets == 5
    assert graded.articles == 5
    assert graded.negative == 4
    assert graded.negative_share == pytest.approx(0.8)
    assert graded.national is True
    assert graded.named is True
    assert graded.points == 7
    assert graded.level == 4


def test_every_input_at_its_maximum_is_the_only_level_five(session, mandate):
    """Ten outlets, national, all negative, named: 3 + 2 + 2 + 1 = 8 points."""
    sources = (
        "FAZ", "Nordkurier", "Badische Zeitung", "PV Magazine", "Solarserver",
        "Merkur", "Wochenblatt", "Kreiszeitung", "Rheinpfalz", "Nordbayern",
    )
    for source in sources:
        trigger = _cover(session, mandate, source=source)

    graded = crisis.severity(session, mandate, trigger)

    assert (graded.outlets, graded.negative, graded.articles) == (10, 10, 10)
    assert graded.points == 8
    assert graded.level == crisis.LEVEL_MAX


def test_a_single_neutral_regional_story_is_the_floor_and_not_zero(session, mandate):
    """One regional outlet, neutral, the mandate not named: no points at all.

    The level is still 1. A declared crisis is never level zero — somebody
    decided it was one, and the arithmetic grades it, it does not overrule it.
    """
    trigger = _cover(
        session,
        mandate,
        source="Nordkurier",
        title=_MARKET_HEADLINE,
        tonality=Tonality.NEUTRAL,
    )

    graded = crisis.severity(session, mandate, trigger)

    assert (graded.outlets, graded.national, graded.named) == (1, False, False)
    assert graded.points == 0
    assert graded.level == crisis.LEVEL_MIN


def test_the_level_never_asks_a_model(session, mandate):
    """The structural half of the same claim.

    ``severity`` takes no ``invoke``, no analyzer and no generator, and the module
    does not import one — so there is no seam through which an estimate could
    reach the number, however the call site is later written.
    """
    import inspect

    from newspulse import crisis as module

    source = inspect.getsource(module)
    for forbidden in ("analyzer", "gemini", "invoke_claude_cli", "brain"):
        assert f"from .{forbidden}" not in source
        assert f" {forbidden}," not in source.split("_log = ")[0]

    signature = inspect.signature(module.severity)
    assert list(signature.parameters) == ["session", "client", "article"]


# --- The proposal ---------------------------------------------------------------


def test_a_krise_analysis_at_importance_eight_proposes(session, mandate):
    _cover(
        session,
        mandate,
        source="Nordkurier",
        category=Category.KRISE,
        importance=crisis.PROPOSAL_IMPORTANCE,
    )

    proposal = crisis.propose(session, mandate, now=_NOW)

    assert proposal is not None
    assert proposal.trigger is crisis.Trigger.KATEGORIE
    assert proposal.headline == _wording("Nordkurier")


def test_a_krise_analysis_below_the_importance_does_not_propose(session, mandate):
    """Seven is the alert threshold; the proposal bar sits deliberately above it,
    so a bad news day is not a crisis question every morning."""
    _cover(
        session,
        mandate,
        source="Nordkurier",
        category=Category.KRISE,
        importance=crisis.PROPOSAL_IMPORTANCE - 1,
    )

    assert crisis.propose(session, mandate, now=_NOW) is None


def test_three_outlets_carrying_one_story_negatively_propose(session, mandate):
    for source in ("Nordkurier", "Badische Zeitung", "PV Magazine"):
        _cover(session, mandate, source=source, importance=5)

    proposal = crisis.propose(session, mandate, now=_NOW)

    assert proposal is not None
    assert proposal.trigger is crisis.Trigger.WELLE
    assert proposal.outlets == crisis.PROPOSAL_OUTLETS


def test_two_outlets_are_not_a_wave(session, mandate):
    """Two can be a wire copy and its pickup. Three is a wave."""
    for source in ("Nordkurier", "Badische Zeitung"):
        _cover(session, mandate, source=source, importance=5)

    assert crisis.propose(session, mandate, now=_NOW) is None


def test_three_outlets_of_which_one_is_positive_are_not_a_wave(session, mandate):
    """Only the negative members count. Three outlets on a story two of them
    praise is not a wave against the mandate."""
    _cover(session, mandate, source="Nordkurier", importance=5)
    _cover(session, mandate, source="Badische Zeitung", importance=5)
    _cover(
        session, mandate, source="PV Magazine", tonality=Tonality.POSITIV, importance=5
    )

    assert crisis.propose(session, mandate, now=_NOW) is None


def test_a_wave_spread_over_more_than_a_day_does_not_propose(session, mandate):
    """"Drei Medien innerhalb von 24 Stunden" is a window, not a running total."""
    for source in ("Nordkurier", "Badische Zeitung"):
        _cover(session, mandate, source=source, importance=5)
    _cover(
        session,
        mandate,
        source="PV Magazine",
        importance=5,
        published=_NOW - dt.timedelta(hours=30),
    )

    assert crisis.propose(session, mandate, now=_NOW) is None


def test_a_proposal_writes_nothing_at_all(session, mandate):
    """DEC-1 option A in one assertion. No crisis row, no changed cadence, no
    stored anything — a proposal that wrote would be option B under option A's
    name, and a false alarm would cost a morning instead of a click."""
    _cover(
        session,
        mandate,
        source="Nordkurier",
        category=Category.KRISE,
        importance=crisis.PROPOSAL_IMPORTANCE,
    )
    before = (_count(session, Crisis), _count(session, Analysis), _count(session, Article))

    assert crisis.propose(session, mandate, now=_NOW) is not None

    assert (_count(session, Crisis), _count(session, Analysis), _count(session, Article)) == before
    assert crisis.due(session, now=_NOW) == []


def test_a_mandate_already_in_a_crisis_gets_no_proposal(session, mandate):
    """There is at most one open crisis per mandate, so there is nothing to
    offer — the page shows the crisis, not an invitation to declare it again."""
    trigger = _cover(
        session,
        mandate,
        source="Nordkurier",
        category=Category.KRISE,
        importance=crisis.PROPOSAL_IMPORTANCE,
    )
    crisis.declare(session, mandate, trigger, by="lucas", now=_NOW)

    assert crisis.propose(session, mandate, now=_NOW) is None


def test_a_mandate_with_no_coverage_gets_no_proposal(session, mandate):
    assert crisis.propose(session, mandate, now=_NOW) is None


# --- Declaring and closing ------------------------------------------------------


def test_declare_records_the_trigger_the_person_and_the_moment(session, mandate):
    trigger = _cover(session, mandate, source="FAZ")

    declared = crisis.declare(session, mandate, trigger, by="lucas", now=_NOW)

    assert declared.article_id == trigger.id
    assert declared.declared_by == "lucas"
    assert declared.declared_at == _NOW
    assert declared.closed_at is None
    assert declared.close_reason == ""
    assert crisis.LEVEL_MIN <= declared.level <= crisis.LEVEL_MAX


def test_declare_stores_the_counts_the_level_was_computed_from(session, mandate):
    """The number and its arithmetic travel together, or the number is a claim."""
    for source in ("FAZ", "Nordkurier", "Badische Zeitung"):
        trigger = _cover(session, mandate, source=source)

    declared = crisis.declare(session, mandate, trigger, by="lucas", now=_NOW)

    assert declared.outlet_count == 3
    assert declared.article_count == 3
    assert declared.negative_count == 3
    assert declared.national is True
    assert declared.named is True


def test_a_declaration_without_a_name_is_still_recorded_as_a_person(session, mandate):
    trigger = _cover(session, mandate, source="FAZ")

    declared = crisis.declare(session, mandate, trigger, by="  ", now=_NOW)

    assert declared.declared_by == crisis.DECLARED_BY_DEFAULT


def test_a_second_declaration_returns_the_standing_crisis(session, mandate):
    """Not a second row: a double click, a second tab and a restart mid-click all
    have to land on the same crisis."""
    first_article = _cover(session, mandate, source="FAZ")
    second_article = _cover(session, mandate, source="Nordkurier")
    first = crisis.declare(session, mandate, first_article, by="lucas", now=_NOW)

    again = crisis.declare(
        session, mandate, second_article, by="raphael", now=_NOW + dt.timedelta(hours=1)
    )

    assert again.id == first.id
    assert again.declared_by == "lucas"
    assert _count(session, Crisis) == 1


def test_the_database_refuses_a_second_open_crisis_outright(session, mandate):
    """The partial unique index, not the guard in ``declare``. Two processes can
    reach the write at the same moment; only the schema can settle that."""
    trigger = _cover(session, mandate, source="FAZ")
    crisis.declare(session, mandate, trigger, by="lucas", now=_NOW)

    session.add(
        Crisis(
            client_id=mandate.id,
            article_id=trigger.id,
            declared_by="raphael",
            declared_at=_NOW,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_close_demands_a_reason(session, mandate):
    trigger = _cover(session, mandate, source="FAZ")
    declared = crisis.declare(session, mandate, trigger, by="lucas", now=_NOW)

    with pytest.raises(ValueError):
        crisis.close(session, declared, reason="   ", now=_NOW)

    assert declared.closed_at is None


def test_close_ends_the_crisis_and_leaves_the_row_readable(session, mandate):
    trigger = _cover(session, mandate, source="FAZ")
    declared = crisis.declare(session, mandate, trigger, by="lucas", now=_NOW)
    ended = _NOW + dt.timedelta(hours=6)

    crisis.close(session, declared, reason="Abmahnung zurueckgezogen", now=ended)

    assert declared.closed_at == ended
    assert declared.close_reason == "Abmahnung zurueckgezogen"
    # Still there, still complete: the closed crisis is its own review document.
    stored = session.scalars(select(Crisis)).one()
    assert stored.declared_by == "lucas"
    assert stored.article_id == trigger.id


def test_a_closed_crisis_lets_the_next_one_be_declared(session, mandate):
    """The index is partial for this reason: a mandate may have had five crises
    and be in none."""
    trigger = _cover(session, mandate, source="FAZ")
    first = crisis.declare(session, mandate, trigger, by="lucas", now=_NOW)
    crisis.close(session, first, reason="vorbei", now=_NOW + dt.timedelta(hours=2))

    second = crisis.declare(
        session, mandate, trigger, by="lucas", now=_NOW + dt.timedelta(days=3)
    )

    assert second.id != first.id
    assert _count(session, Crisis) == 2


def test_closing_a_closed_crisis_keeps_the_first_reason(session, mandate):
    trigger = _cover(session, mandate, source="FAZ")
    declared = crisis.declare(session, mandate, trigger, by="lucas", now=_NOW)
    ended = _NOW + dt.timedelta(hours=6)
    crisis.close(session, declared, reason="erste Begruendung", now=ended)

    crisis.close(session, declared, reason="zweite Begruendung", now=ended + dt.timedelta(hours=1))

    assert declared.close_reason == "erste Begruendung"
    assert declared.closed_at == ended


# --- The tighter cadence --------------------------------------------------------


def test_a_freshly_declared_crisis_is_due_immediately(session, mandate):
    trigger = _cover(session, mandate, source="FAZ")
    declared = crisis.declare(session, mandate, trigger, by="lucas", now=_NOW)

    assert [row.id for row in crisis.due(session, now=_NOW)] == [declared.id]


def test_the_cadence_is_the_configured_number_of_minutes(session, mandate, monkeypatch):
    monkeypatch.setenv(config.ENV_CRISIS_SWEEP_MINUTES, "30")
    trigger = _cover(session, mandate, source="FAZ")
    declared = crisis.declare(session, mandate, trigger, by="lucas", now=_NOW)
    crisis.mark_swept(session, declared, now=_NOW)

    assert crisis.due(session, now=_NOW + dt.timedelta(minutes=29)) == []
    assert [row.id for row in crisis.due(session, now=_NOW + dt.timedelta(minutes=30))] == [
        declared.id
    ]


def test_the_default_cadence_is_an_hour(monkeypatch):
    monkeypatch.delenv(config.ENV_CRISIS_SWEEP_MINUTES, raising=False)
    assert config.crisis_sweep_minutes() == 60


@pytest.mark.parametrize("value", ["0", "-5", "1", "abrakadabra"])
def test_a_cadence_below_the_floor_is_clamped(monkeypatch, value):
    """Zero would make a crisis reading due on every scheduler tick — a fetch a
    minute against the same feed, for as long as the crisis is open."""
    monkeypatch.setenv(config.ENV_CRISIS_SWEEP_MINUTES, value)
    assert config.crisis_sweep_minutes() >= 5


def test_a_closed_crisis_is_never_due(session, mandate):
    trigger = _cover(session, mandate, source="FAZ")
    declared = crisis.declare(session, mandate, trigger, by="lucas", now=_NOW)
    crisis.close(session, declared, reason="vorbei", now=_NOW)

    assert crisis.due(session, now=_NOW + dt.timedelta(days=1)) == []


def test_the_cadence_state_is_read_from_the_table_not_from_an_object(
    factory, session, mandate
):
    """A restart is exactly this: a different session, holding nothing."""
    trigger = _cover(session, mandate, source="FAZ")
    declared = crisis.declare(session, mandate, trigger, by="lucas", now=_NOW)
    crisis.mark_swept(session, declared, now=_NOW)

    with factory() as after_restart:
        assert crisis.due(after_restart, now=_NOW + dt.timedelta(minutes=5)) == []
        still_open = crisis.open_crisis(after_restart, mandate)
        assert still_open is not None
        assert still_open.last_swept_at == _NOW


# --- The crisis reading itself --------------------------------------------------


def _news_item(title: str, source: str, url: str) -> FeedItem:
    return FeedItem(
        title=title,
        link=url,
        source=source,
        published_at=_NOW - dt.timedelta(hours=1),
        summary="Kurz zusammengefasst.",
        language="de",
    )


def test_a_crisis_reading_touches_only_the_affected_mandates_sources(session, mandate):
    """"Liest ausschließlich die Quellen des betroffenen Mandats" — the other
    mandate's feed is not fetched, and nothing is stored for it."""
    other = Client(name="Windkraft Nord GmbH", industry="Windenergie")
    session.add(other)
    session.commit()
    trigger = _cover(session, mandate, source="FAZ")
    declared = crisis.declare(session, mandate, trigger, by="lucas", now=_NOW)

    mine = gnews.client_feeds([mandate])[0].url
    theirs = gnews.client_feeds([other])[0].url
    seen: list[str] = []
    analyzer = _FakeAnalyzer()
    fetched = {
        mine: [_news_item("Solaris AG unter Druck bei der Verbraucherzentrale",
                          "Nordkurier", "https://nk.example.de/neu")],
        theirs: [_news_item("Windkraft Nord GmbH baut aus", "FAZ",
                            "https://faz.example.de/wk")],
    }

    sweep = job.run_crisis(
        session,
        declared,
        analyzer=analyzer,
        fetch=_fetch_recording(fetched, seen),
        now=lambda: _NOW,
    )

    assert seen == [mine]
    assert sweep.articles == 1
    assert [name for name, _ in analyzer.calls] == ["Solaris AG"]
    assert (
        session.scalar(
            select(func.count()).select_from(Analysis).where(Analysis.client_id == other.id)
        )
        == 0
    )


def test_a_crisis_reading_writes_no_draft_no_profile_and_no_run_row(session, mandate):
    """The whole of "der engere Takt ist eng begrenzt", in one assertion each.

    The runs row is the subtle one: ``_determine_since`` takes the last run's
    start as the next sweep's watermark, so an hourly single-mandate reading
    recorded as a run would tell tomorrow's sweep the whole portfolio was already
    covered.
    """
    trigger = _cover(session, mandate, source="FAZ")
    declared = crisis.declare(session, mandate, trigger, by="lucas", now=_NOW)
    mine = gnews.client_feeds([mandate])[0].url
    seen: list[str] = []

    job.run_crisis(
        session,
        declared,
        analyzer=_FakeAnalyzer(),
        fetch=_fetch_recording(
            {mine: [_news_item("Solaris AG raeumt Fehler ein", "Nordkurier",
                               "https://nk.example.de/2")]},
            seen,
        ),
        now=lambda: _NOW,
    )

    assert _count(session, Angle) == 0
    assert _count(session, Run) == 0
    assert mandate.profile_checked_at is None
    assert mandate.profile_note == ""


def test_a_crisis_reading_regrades_from_what_it_just_stored(session, mandate):
    """The level is arithmetic all the way through: coverage that arrives during
    a crisis moves it, and nothing else does."""
    first = _cover(session, mandate, source="Nordkurier")
    declared = crisis.declare(session, mandate, first, by="lucas", now=_NOW)
    assert declared.outlet_count == 1
    mine = gnews.client_feeds([mandate])[0].url

    job.run_crisis(
        session,
        declared,
        analyzer=_FakeAnalyzer(tonality=Tonality.NEGATIV),
        fetch=_fetch_recording(
            {
                mine: [
                    _news_item(
                        _wording("FAZ"), "FAZ", "https://faz.example.de/s1"
                    ),
                    _news_item(
                        _wording("Badische Zeitung"),
                        "Badische Zeitung",
                        "https://bz.example.de/s1",
                    ),
                ]
            },
            [],
        ),
        now=lambda: _NOW,
    )

    assert declared.outlet_count == 3
    assert declared.national is True
    assert declared.level > 1


def test_a_reading_that_finds_nothing_still_moves_the_clock(session, mandate):
    """Otherwise a mandate whose feed has gone quiet is due on every tick."""
    trigger = _cover(session, mandate, source="FAZ")
    declared = crisis.declare(session, mandate, trigger, by="lucas", now=_NOW)

    job.run_crisis(
        session,
        declared,
        analyzer=_FakeAnalyzer(),
        fetch=_fetch_recording({}, []),
        now=lambda: _NOW,
    )

    assert declared.last_swept_at == _NOW
    assert crisis.due(session, now=_NOW + dt.timedelta(minutes=5)) == []


def test_a_crash_mid_reading_leaves_no_hanging_crisis_and_no_second_run(
    factory, session, mandate
):
    """The process is killed between the stamp and the storing.

    ``KeyboardInterrupt`` rather than an ``Exception``, deliberately: every fetch
    and analysis boundary in the sweep catches ``Exception`` on purpose, so an
    ordinary error would prove nothing about a *crash*. Read back through a
    second session, because the claim is about the table and not about the object
    the dead thread was holding.
    """
    trigger = _cover(session, mandate, source="FAZ")
    declared = crisis.declare(session, mandate, trigger, by="lucas", now=_NOW)

    def _killed(*args, **kwargs):
        raise KeyboardInterrupt("the process went away")

    with pytest.raises(KeyboardInterrupt):
        job.run_crisis(
            session, declared, analyzer=_FakeAnalyzer(), fetch=_killed, now=lambda: _NOW
        )
    session.rollback()

    with factory() as after_restart:
        assert _count(after_restart, Crisis) == 1
        standing = crisis.open_crisis(after_restart, mandate)
        assert standing is not None, "the crisis survived the crash"
        assert standing.closed_at is None, "and it is not hung in some third state"
        # The clock moved before the reading, so the next tick does not start a
        # second one on top of the one that died.
        assert crisis.due(after_restart, now=_NOW + dt.timedelta(minutes=5)) == []
        assert crisis.due(after_restart, now=_NOW + dt.timedelta(minutes=61)) != []


# --- The scheduler's second clock -----------------------------------------------


@pytest.fixture
def scheduled(factory, monkeypatch):
    """The scheduler, pointed at this test's database instead of the real one."""
    from contextlib import contextmanager

    from newspulse.web import scheduler

    @contextmanager
    def _session():
        with factory() as open_session:
            yield open_session

    monkeypatch.setattr(scheduler, "get_session", _session)
    monkeypatch.setattr(scheduler.job, "setup_logging", lambda *a, **k: None)
    return scheduler


def _reading_recorder(read: list[int]):
    """A stand-in for ``job.run_crisis`` that records which crisis it was given."""

    def _run(_session, row, **_kwargs) -> job.CrisisSweep:
        read.append(row.id)
        return job.CrisisSweep(articles=0, analyses=0, level=row.level, errors=[])

    return _run


def test_the_tick_reads_every_due_crisis_and_nothing_else(
    scheduled, session, mandate, monkeypatch
):
    """The cadence's whole contract: one reading per open, due crisis."""
    quiet = Client(name="Windkraft Nord GmbH", industry="Windenergie")
    session.add(quiet)
    session.commit()
    trigger = _cover(session, mandate, source="FAZ")
    declared = crisis.declare(session, mandate, trigger, by="lucas", now=_NOW)

    read: list[int] = []
    monkeypatch.setattr(scheduled.job, "run_crisis", _reading_recorder(read))

    scheduled._crisis_tick(_NOW)

    assert read == [declared.id]


def test_the_tick_does_nothing_when_no_crisis_is_open(
    scheduled, session, mandate, monkeypatch
):
    """No crisis, no cadence: the sweep's rhythm is unchanged by this module
    existing."""
    _cover(session, mandate, source="FAZ")
    read: list[int] = []
    monkeypatch.setattr(scheduled.job, "run_crisis", _reading_recorder(read))

    scheduled._crisis_tick(_NOW)

    assert read == []


def test_the_tick_gives_way_to_a_sweep_that_is_already_running(
    scheduled, session, mandate, monkeypatch
):
    """Non-blocking on the run guard. Queueing behind a full portfolio sweep
    would fetch the same feeds twice in a row, and the next tick is a minute
    away."""
    trigger = _cover(session, mandate, source="FAZ")
    crisis.declare(session, mandate, trigger, by="lucas", now=_NOW)
    read: list[int] = []
    monkeypatch.setattr(scheduled.job, "run_crisis", _reading_recorder(read))

    assert scheduled.runlock.guard.acquire(blocking=False)
    try:
        scheduled._crisis_tick(_NOW)
    finally:
        scheduled.runlock.guard.release()

    assert read == []


def test_a_crisis_already_read_this_hour_is_not_read_again(
    scheduled, session, mandate, monkeypatch
):
    """The tick is a minute and the cadence is an hour, so all but one tick in
    sixty has to decide to do nothing — off the row, not off a timer."""
    trigger = _cover(session, mandate, source="FAZ")
    declared = crisis.declare(session, mandate, trigger, by="lucas", now=_NOW)
    crisis.mark_swept(session, declared, now=_NOW)
    read: list[int] = []
    monkeypatch.setattr(scheduled.job, "run_crisis", _reading_recorder(read))

    scheduled._crisis_tick(_NOW + dt.timedelta(minutes=1))
    scheduled._crisis_tick(_NOW + dt.timedelta(minutes=59))

    assert read == []

    scheduled._crisis_tick(_NOW + dt.timedelta(minutes=60))

    assert read == [declared.id]


def test_the_crisis_loop_survives_a_failing_reading(scheduled, monkeypatch):
    """A crisis is the worst moment for the tool to go quiet, so the same rule as
    the daily loop and for the same reason: log it and be back in a minute."""
    import threading

    attempts: list[int] = []

    def _boom(_now):
        attempts.append(1)
        raise RuntimeError("Feed-Anbieter weg")

    monkeypatch.setattr(scheduled, "_TICK_SECONDS", 0.01)
    monkeypatch.setattr(scheduled, "_crisis_tick", _boom)

    stop = threading.Event()
    thread = threading.Thread(target=scheduled._crisis_loop, args=(stop,), daemon=True)
    thread.start()
    for _ in range(300):
        if len(attempts) >= 2:
            break
        thread.join(0.01)
    still_running = thread.is_alive()
    stop.set()
    thread.join(1)

    assert len(attempts) >= 2, "it tried again after the failure"
    assert still_running, "the thread was alive until we asked it to stop"
