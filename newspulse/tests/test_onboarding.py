"""A newly created client arrives with coverage (newspulse.job.backfill_client).

An empty mandate on the day it is created was the complaint that started this: the
daily sweep only fetches what is new, so a company added on Thursday shows nothing
until somebody writes about it on Friday. The onboarding fetch closes that gap with
a hard cap on articles — the cap, not a date window, is what bounds the cost, since
every article stored here goes through the analyzer.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from newspulse import job
from newspulse.db import make_engine
from newspulse.ingest import FeedItem
from newspulse.models import Analysis, Article, Base, Category, Client, Run
from newspulse.schemas import Analysis as AnalysisSchema

_NOW = dt.datetime(2026, 7, 30, 12, 0, tzinfo=dt.UTC)


@pytest.fixture
def session():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as sess:
        yield sess


def _client(session, name="Arrakis Finance", **over) -> Client:
    client = Client(
        name=name,
        aliases=over.get("aliases", ["Arrakis"]),
        keywords=over.get("keywords", []),
        alert_topics=[],
        country="DE",
    )
    session.add(client)
    session.commit()
    return client


def _items(count: int, *, newest: dt.datetime = _NOW) -> list[FeedItem]:
    """``count`` items, one day apart, newest first.

    Each names the client, because the onboarding fetch runs the same recall
    pre-filter as the sweep: the feed is a name search, so its results normally
    do carry the name, and anything that does not is noise the filter is there
    to drop before it reaches the analyzer.
    """
    return [
        FeedItem(
            title=f"Arrakis Meldung {i}",
            link=f"https://example.de/{i}",
            source="cash.at",
            published_at=newest - dt.timedelta(days=i),
            summary="Ein Satz.",
            language="de",
        )
        for i in range(count)
    ]


def _fetch_returning(items: list[FeedItem]) -> job.FetchFeed:
    def _fetch(url, since, *, source=None, fetched_at=None, **_):
        return [item for item in items if item.published_at >= since]

    return _fetch


class _FakeAnalyzer:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def analyze(self, client, articles):
        self.calls.append((client.id, len(articles)))
        return [
            AnalysisSchema(
                article_id=article.id,
                client_id=client.id,
                is_relevant=True,
                summary="s",
                category=Category.PRODUKT,
                relevance_score=6,
                importance_score=5,
                is_alert=False,
                reasoning="r",
            )
            for article in articles
        ]


def test_a_new_client_arrives_with_coverage(session):
    client = _client(session)
    analyzer = _FakeAnalyzer()

    stored = job.backfill_client(
        session, client, analyzer=analyzer, fetch=_fetch_returning(_items(5)), now=lambda: _NOW
    )

    assert stored == 5
    assert session.scalar(select(func.count()).select_from(Article)) == 5
    assert session.scalar(select(func.count()).select_from(Analysis)) == 5
    assert analyzer.calls == [(client.id, 5)]


def test_the_cap_is_on_articles_and_keeps_the_newest(session):
    """Thirty is what bounds the cost — every one of them is analysed."""
    client = _client(session)

    job.backfill_client(
        session, client, analyzer=_FakeAnalyzer(),
        fetch=_fetch_returning(_items(50)), now=lambda: _NOW,
    )

    titles = session.scalars(select(Article.title)).all()
    assert len(titles) == job.ONBOARDING_ARTICLES
    # "The last 30" is a recency promise: the newest thirty, not whichever thirty
    # the feed happened to list first.
    assert "Arrakis Meldung 0" in titles
    assert "Arrakis Meldung 29" in titles
    assert "Arrakis Meldung 30" not in titles


def test_no_run_row_is_written(session):
    """Load-bearing: the sweep's watermark is the last successful run's start.

    Recording this narrow single-client fetch as a run would tell the next daily
    sweep that everything up to now was already covered, and the rest of the
    portfolio would silently lose a day of coverage.
    """
    client = _client(session)

    job.backfill_client(
        session, client, analyzer=_FakeAnalyzer(),
        fetch=_fetch_returning(_items(3)), now=lambda: _NOW,
    )

    assert session.scalar(select(func.count()).select_from(Run)) == 0


def test_only_this_client_is_matched_and_analysed(session):
    """Onboarding one mandate must not fan out over the portfolio."""
    newcomer = _client(session, name="Arrakis Finance")
    _client(session, name="Zalando", aliases=[])
    analyzer = _FakeAnalyzer()

    job.backfill_client(
        session, newcomer, analyzer=analyzer,
        fetch=_fetch_returning(_items(3)), now=lambda: _NOW,
    )

    assert [call[0] for call in analyzer.calls] == [newcomer.id]


def test_articles_already_in_the_archive_are_not_stored_twice(session):
    """Runs through the same dedup as the sweep, so re-onboarding costs nothing."""
    client = _client(session)
    fetch = _fetch_returning(_items(4))

    job.backfill_client(session, client, analyzer=_FakeAnalyzer(), fetch=fetch, now=lambda: _NOW)
    second = job.backfill_client(
        session, client, analyzer=_FakeAnalyzer(), fetch=fetch, now=lambda: _NOW
    )

    assert second == 0
    assert session.scalar(select(func.count()).select_from(Article)) == 4


def test_a_client_nobody_writes_about_is_not_an_error(session):
    """The realistic case for a young company: the search returns nothing.

    An empty result is a fact about the market, not a failure — it must leave the
    mandate created and the log honest rather than raising into the form.
    """
    client = _client(session, name="Nagelneu GmbH", aliases=[])

    stored = job.backfill_client(
        session, client, analyzer=_FakeAnalyzer(), fetch=_fetch_returning([]), now=lambda: _NOW
    )

    assert stored == 0
    assert session.scalar(select(func.count()).select_from(Article)) == 0


def test_a_client_without_a_usable_name_is_skipped(session):
    """No search feed can be built, so there is nothing to fetch and no crash."""
    client = _client(session, name="   ", aliases=[])

    assert job.backfill_client(session, client, analyzer=_FakeAnalyzer(), now=lambda: _NOW) == 0


def test_a_dead_feed_does_not_take_the_creation_down_with_it(session):
    """The fetch is fault-isolated per feed, like the sweep's."""
    client = _client(session)

    def _explode(url, since, **_):
        raise RuntimeError("Google ist weg")

    assert job.backfill_client(
        session, client, analyzer=_FakeAnalyzer(), fetch=_explode, now=lambda: _NOW
    ) == 0


# --- The market it sits in -----------------------------------------------------
#
# The case that matters most for a young company: nobody writes about it yet, but
# its subject is being discussed. Those articles are what a positioning statement
# is made of, and without this the mandate waits until the next nightly sweep for
# any of it.


def _market_items(count: int, *, newest: dt.datetime = _NOW) -> list[FeedItem]:
    """Market coverage that never names the client — the whole point of it."""
    return [
        FeedItem(
            title=f"Kryptoboerse stellt Betrieb ein {i}",
            link=f"https://example.de/markt/{i}",
            source="yellow.com",
            published_at=newest - dt.timedelta(days=i),
            summary="Ein Handelsplatz kuendigt die Abwicklung an.",
            language="de",
        )
        for i in range(count)
    ]


def _split_fetch(own: list[FeedItem], market: list[FeedItem]) -> job.FetchFeed:
    """Answer the name search and the topic radar with different material."""

    def _fetch(url, since, *, source=None, fetched_at=None, **_):
        batch = market if "Themen-Radar" in (source or "") else own
        return [item for item in batch if item.published_at >= since]

    return _fetch


def test_a_client_nobody_writes_about_still_gets_its_market(session, monkeypatch):
    """Arrakis on day one: zero articles about it, plenty about its subject."""
    from newspulse import angles

    monkeypatch.setattr(angles, "suggest", lambda *a, **k: None)
    client = _client(session, keywords=["Onchain-Liquiditaet"])

    stored = job.backfill_client(
        session, client, analyzer=_FakeAnalyzer(),
        fetch=_split_fetch([], _market_items(4)), now=lambda: _NOW,
    )

    # Nothing is *about* the client, which is what the return value counts.
    assert stored == 0
    # But its market is in the archive, ready to be positioned against.
    assert session.scalar(select(func.count()).select_from(Article)) == 4
    # And filed as market, not as coverage: an article that never names the
    # mandate must not appear in its own archive.
    assert session.scalar(select(func.count()).select_from(Analysis)) == 0


def test_the_market_material_can_produce_a_draft_on_day_one(session, monkeypatch):
    """One model call, and the mandate has something to say the day it is created."""
    from newspulse import angles
    from newspulse.models import Angle
    from newspulse.schemas import AngleDraft

    seen: list[int] = []

    def _suggest(sess, cli, material, **_):
        seen.append(len(material))
        numbered = angles.developments(material)
        draft = AngleDraft(
            worth_sending=True,
            subject="Liquiditaet als Infrastruktur",
            message="Zwei Absaetze Text.",
            context="Mehrere Handelsplaetze schliessen.",
            thesis="Der Markt konsolidiert.",
            overclaim="Zentrale Boersen verschwinden.",
            evidence=[0],
        )
        return draft, numbered

    monkeypatch.setattr(angles, "suggest", _suggest)
    client = _client(session, keywords=["Onchain-Liquiditaet"])

    job.backfill_client(
        session, client, analyzer=_FakeAnalyzer(),
        fetch=_split_fetch([], _market_items(3)), now=lambda: _NOW,
    )

    assert seen == [3]
    assert session.scalar(select(func.count()).select_from(Angle)) == 1


def test_a_client_without_themes_gets_no_market_fetch(session):
    """Without themes there is no way to tell which market coverage concerns them."""
    client = _client(session, keywords=[])

    job.backfill_client(
        session, client, analyzer=_FakeAnalyzer(),
        fetch=_split_fetch(_items(2), _market_items(5)), now=lambda: _NOW,
    )

    # Only the two about the client itself; the radar was never asked.
    assert session.scalar(select(func.count()).select_from(Article)) == 2


def test_a_competitor_gets_no_market_fetch(session):
    """Nobody writes a competitor a positioning statement."""
    client = _client(session, keywords=["Onchain-Liquiditaet"])
    client.is_competitor = True
    session.commit()

    job.backfill_client(
        session, client, analyzer=_FakeAnalyzer(),
        fetch=_split_fetch([], _market_items(3)), now=lambda: _NOW,
    )

    assert session.scalar(select(func.count()).select_from(Article)) == 0


def test_a_story_arriving_through_both_feeds_is_stored_once(session, monkeypatch):
    """The name search and the radar overlap; the archive must not hold it twice."""
    from newspulse import angles

    monkeypatch.setattr(angles, "suggest", lambda *a, **k: None)
    client = _client(session, keywords=["Onchain-Liquiditaet"])
    both = _items(2)

    job.backfill_client(
        session, client, analyzer=_FakeAnalyzer(),
        fetch=_split_fetch(both, both), now=lambda: _NOW,
    )

    assert session.scalar(select(func.count()).select_from(Article)) == 2


# --- The wiring ----------------------------------------------------------------


def test_creating_a_client_starts_the_onboarding_fetch(monkeypatch):
    """The route must not wait for it: fetching and analysing takes minutes."""
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from newspulse.web.app import create_app, get_db

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app = create_app()

    def _override():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override

    from newspulse.web.routes import settings as settings_routes

    started: list[str] = []
    # conftest neutralises this globally; here it is the thing under test.
    monkeypatch.setattr(
        settings_routes, "_start_onboarding", lambda cid, name: started.append(name)
    )

    resp = TestClient(app).post(
        "/settings/clients", data={"name": "Arrakis Finance"}, follow_redirects=False
    )

    assert resp.status_code == 303
    assert started == ["Arrakis Finance"]


def test_a_rejected_client_starts_nothing(monkeypatch):
    """A duplicate name never becomes a client, so it must not spend a fetch."""
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from newspulse.web.app import create_app, get_db
    from newspulse.web.routes import settings as settings_routes

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        s.add(Client(name="Arrakis Finance", aliases=[], keywords=[], alert_topics=[]))
        s.commit()

    app = create_app()

    def _override():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override

    started: list[str] = []
    monkeypatch.setattr(
        settings_routes, "_start_onboarding", lambda cid, name: started.append(name)
    )

    TestClient(app).post("/settings/clients", data={"name": "Arrakis Finance"})

    assert started == []


# --- A new mandate must not open on two empty panels -----------------------------


def test_onboarding_drafts_a_position_and_the_message_for_it(monkeypatch):
    """"Sobald ein Mandant angelegt ist, sollte immer ein Impuls und eine
    Empfehlung platziert sein — das sollte nie leer sein."

    Both were only produced by the nightly sweep, so a mandate created at ten in
    the morning showed two empty panels until the next day — a poor first
    impression of a tool whose whole promise is "here is what to say".

    The second half is no longer a "recommendation" panel: it is the letter that
    carries the position to a named recipient, which is the same material doing a
    job instead of describing one.
    """
    import datetime as dt

    from sqlalchemy.orm import sessionmaker

    from newspulse import job, outreach
    from newspulse.db import make_engine
    from newspulse.models import Angle, Base, Client
    from newspulse.schemas import PersonalMessage
    from newspulse.web.routes import settings as settings_routes

    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    called: list[str] = []

    with factory() as session:
        client = Client(
            name="Neu AG", aliases=[], industry="Modehandel",
            keywords=["Retouren"], alert_topics=[], country="DE",
        )
        session.add(client)
        session.flush()
        session.add(
            Angle(
                client_id=client.id, generated_at=dt.datetime.now(dt.UTC),
                subject="Retouren als Kostenfrage", message="Zwei Absätze.",
                context="c", thesis="Die Quote ist eine Prozessfrage.",
            )
        )
        session.commit()

        monkeypatch.setattr(
            job, "link_archive_to_themes",
            lambda *a, **k: called.append("linked") or 0,
        )
        monkeypatch.setattr(
            job, "_refresh_impulses", lambda *a, **k: called.append("impulse") or 1
        )
        monkeypatch.setattr(
            outreach, "draft",
            lambda *a, **k: (
                called.append("wrote") or PersonalMessage(message="Sehr geehrte…")
            ),
        )
        monkeypatch.setattr(outreach, "store", lambda *a, **k: called.append("stored"))

        settings_routes._first_drafts(session, client)

    assert called == ["linked", "impulse", "wrote", "stored"]


def test_a_failing_half_never_takes_the_other_with_it(monkeypatch):
    """A mandate must still arrive if one draft fails."""
    from sqlalchemy.orm import sessionmaker

    from newspulse import job
    from newspulse.db import make_engine
    from newspulse.models import Base, Client
    from newspulse.web.routes import settings as settings_routes

    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    reached: list[str] = []

    def _boom(*a, **k):
        raise RuntimeError("claude ist weg")

    with factory() as session:
        client = Client(name="Neu AG", aliases=[], keywords=[], alert_topics=[])
        session.add(client)
        session.commit()
        monkeypatch.setattr(job, "link_archive_to_themes", _boom)
        monkeypatch.setattr(
            job, "_refresh_impulses", lambda *a, **k: reached.append("impulse") or 0
        )

        settings_routes._first_drafts(session, client)  # must not raise

    assert reached == ["impulse"]


def test_no_message_is_written_without_a_position_to_carry(monkeypatch):
    """The letter personalises a thesis. With no impulse there is no thesis, and
    a letter with none in it is a form — so the model is never asked."""
    from sqlalchemy.orm import sessionmaker

    from newspulse import job, outreach
    from newspulse.db import make_engine
    from newspulse.models import Base, Client
    from newspulse.web.routes import settings as settings_routes

    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    with factory() as session:
        client = Client(name="Still AG", aliases=[], keywords=[], alert_topics=[])
        session.add(client)
        session.commit()
        monkeypatch.setattr(job, "link_archive_to_themes", lambda *a, **k: 0)
        monkeypatch.setattr(job, "_refresh_impulses", lambda *a, **k: 0)
        monkeypatch.setattr(
            outreach, "draft", lambda *a, **k: pytest.fail("must not ask the model")
        )

        settings_routes._first_drafts(session, client)



def test_a_new_mandate_is_given_a_measured_radar(monkeypatch, no_theme_settling):
    """Beta-tested by adding "Google" through the real form with the theme field
    empty, which is what anyone does the first time: a name, a website, and the
    reasonable expectation that the tool works out the rest.

    It classified the industry, fetched and analysed thirty articles, and then the
    Impulse page said "Dafür braucht dieser Mandant Themen" — the promise that a
    new mandate is never empty, broken by the one field the operator was least
    likely to fill in.
    """
    from sqlalchemy.orm import sessionmaker

    from newspulse import themes
    from newspulse.db import make_engine
    from newspulse.models import Base, Client
    from newspulse.themes import ThemeProbe
    from newspulse.web.routes import settings as settings_routes

    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    with factory() as session:
        client = Client(name="Google", aliases=[], industry="Suchmaschinen",
                        keywords=[], alert_topics=[], country="DE")
        session.add(client)
        session.commit()

        monkeypatch.setattr(themes, "suggest", lambda c, **k: ["egal"])
        monkeypatch.setattr(
            themes, "probe",
            lambda c, proposals, **k: [
                ThemeProbe(term="Digital Markets Act", reason="", external=9, own=0),
                # Measured and found wanting: the press does not write this one, and
                # a theme nobody writes filters everything away.
                ThemeProbe(term="Synergetische Suchintelligenz", reason="", external=0, own=0),
                ThemeProbe(term="Generative KI", reason="", external=14, own=1),
            ],
        )

        no_theme_settling(session, client)  # the real one

        assert client.keywords == ["Digital Markets Act", "Generative KI"]


def test_a_mandate_that_brought_its_own_themes_is_left_alone(monkeypatch, no_theme_settling):
    """The operator's own terms are the point; overwriting them with proposals
    would silently change what the radar watches."""
    from sqlalchemy.orm import sessionmaker

    from newspulse import themes
    from newspulse.db import make_engine
    from newspulse.models import Base, Client
    from newspulse.web.routes import settings as settings_routes

    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    with factory() as session:
        client = Client(name="Qonto", aliases=[], keywords=["Firmenkunden-Banking"],
                        alert_topics=[])
        session.add(client)
        session.commit()
        monkeypatch.setattr(
            themes, "suggest", lambda *a, **k: pytest.fail("must not ask the model")
        )

        no_theme_settling(session, client)  # the real one

        assert client.keywords == ["Firmenkunden-Banking"]


def test_no_usable_theme_leaves_the_radar_empty_rather_than_wrong(monkeypatch, no_theme_settling):
    """A mandate silently configured with three terms the press never writes is
    worse off than one with none: the emptiness now looks like the market's
    fault."""
    from sqlalchemy.orm import sessionmaker

    from newspulse import themes
    from newspulse.db import make_engine
    from newspulse.models import Base, Client
    from newspulse.themes import ThemeProbe
    from newspulse.web.routes import settings as settings_routes

    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    with factory() as session:
        client = Client(name="Nischen AG", aliases=[], keywords=[], alert_topics=[])
        session.add(client)
        session.commit()
        monkeypatch.setattr(themes, "suggest", lambda c, **k: ["egal"])
        monkeypatch.setattr(
            themes, "probe",
            lambda c, p, **k: [ThemeProbe(term="Nischenthema", reason="", external=0, own=0)],
        )

        no_theme_settling(session, client)  # the real one

        assert client.keywords == []


def test_the_sweep_gives_an_existing_themeless_mandate_a_radar(monkeypatch):
    """"Hier wird immer noch kein Impuls angezeigt", three times over the same
    mandate.

    Theme settling only ever ran at onboarding, so every mandate created before it
    existed sat permanently in the state onboarding prevents: no themes, therefore
    no radar, therefore no market material, therefore no impulse and no letter —
    and each of those layers reported the one below it as the cause.
    """
    from sqlalchemy.orm import sessionmaker

    from newspulse import job, themes
    from newspulse.db import make_engine
    from newspulse.models import Base, Client

    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    asked: list[str] = []

    with factory() as session:
        old = Client(name="IB-7 Beauty Tech", aliases=[], industry="Beauty Tech",
                     keywords=[], alert_topics=[])
        rival = Client(name="Wettbewerb AG", aliases=[], keywords=[],
                       alert_topics=[], is_competitor=True)
        session.add_all([old, rival])
        session.commit()

        monkeypatch.setattr(
            themes, "settle",
            lambda s_, c, **k: asked.append(c.name) or [],
        )
        job.run(session, analyzer=_NullAnalyzer(), feeds=[], fetch=lambda *a, **k: [])

    # Every mandate, and never a yardstick: a competitor has no impulse page for
    # a radar to fill.
    assert asked == ["IB-7 Beauty Tech"]


class _NullAnalyzer:
    """A sweep needs an analyzer; this one is never reached with no feeds."""

    def analyze(self, *args, **kwargs):  # pragma: no cover - defensive
        return []


def test_a_mandate_that_yields_nothing_is_not_asked_again_tomorrow(monkeypatch, no_theme_settling):
    """The sweep calls this nightly and the guard at the top only fires once a
    radar exists, so a mandate for which the model proposes nothing the press
    writes would cost one model call and up to eight live searches every night,
    forever. Measured in review, not in production, which is the good case.
    """
    from sqlalchemy.orm import sessionmaker

    from newspulse import themes
    from newspulse.db import make_engine
    from newspulse.models import Base, Client
    from newspulse.themes import ThemeProbe

    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    calls: list[str] = []

    with factory() as session:
        client = Client(name="Nischen AG", aliases=[], keywords=[], alert_topics=[])
        session.add(client)
        session.commit()
        monkeypatch.setattr(
            themes, "suggest", lambda c, **k: calls.append("asked") or ["egal"]
        )
        monkeypatch.setattr(
            themes, "probe",
            lambda c, p, **k: [ThemeProbe(term="Nischenthema", reason="", external=0, own=0)],
        )

        day = dt.datetime(2026, 8, 16, 6, 0, tzinfo=dt.UTC)
        no_theme_settling(session, client, now=day)
        no_theme_settling(session, client, now=day + dt.timedelta(days=1))
        no_theme_settling(session, client, now=day + dt.timedelta(days=3))

        assert calls == ["asked"], "asked once, then left alone"

        # A week later it is worth another look: markets acquire vocabulary.
        no_theme_settling(session, client, now=day + dt.timedelta(days=8))

        assert calls == ["asked", "asked"]


def test_a_mandate_that_settles_is_never_asked_again(monkeypatch, no_theme_settling):
    """The cheap guard, and the one that matters in the steady state."""
    from sqlalchemy.orm import sessionmaker

    from newspulse import themes
    from newspulse.db import make_engine
    from newspulse.models import Base, Client
    from newspulse.themes import ThemeProbe

    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    calls: list[str] = []

    with factory() as session:
        client = Client(name="Neu AG", aliases=[], keywords=[], alert_topics=[])
        session.add(client)
        session.commit()
        monkeypatch.setattr(
            themes, "suggest", lambda c, **k: calls.append("asked") or ["egal"]
        )
        monkeypatch.setattr(
            themes, "probe",
            lambda c, p, **k: [ThemeProbe(term="Marktthema", reason="", external=11, own=0)],
        )

        assert no_theme_settling(session, client) == ["Marktthema"]
        assert no_theme_settling(session, client) == []
        assert calls == ["asked"]


def test_the_sweep_really_produces_a_radar_end_to_end(monkeypatch, no_theme_settling):
    """The integration nobody was testing.

    The other sweep test replaces ``settle`` with a recorder and proves only that
    the loop skips competitors — the function name promises a radar and the
    assertion never checks for one. This one patches the two *outside* calls the
    real function makes and drives the whole thing through ``job.run``, which is
    where the injected fetch, the transaction and the guard all meet.
    """
    from sqlalchemy.orm import sessionmaker

    from newspulse import job, themes
    from newspulse.db import make_engine
    from newspulse.models import Base, Client
    from newspulse.themes import ThemeProbe

    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    with factory() as session:
        session.add(Client(name="IB-7 Beauty Tech", aliases=[], industry="Beauty Tech",
                           keywords=[], alert_topics=[]))
        session.commit()

        monkeypatch.setattr(themes, "settle", no_theme_settling)  # the real one
        monkeypatch.setattr(themes, "suggest", lambda c, **k: ["egal"])
        monkeypatch.setattr(
            themes, "probe",
            lambda c, p, **k: [ThemeProbe(term="KI in der Kosmetik", reason="",
                                          external=12, own=0)],
        )
        job.run(session, analyzer=_NullAnalyzer(), feeds=[], fetch=lambda *a, **k: [])

        session.expire_all()
        stored = session.scalars(__import__("sqlalchemy").select(Client)).one()
        assert stored.keywords == ["KI in der Kosmetik"]


def test_a_settling_failure_leaves_the_session_usable(monkeypatch, no_theme_settling):
    """A caught exception is not a clean session.

    ``settle`` writes, so a failed flush leaves the transaction in
    ``PendingRollbackError`` and every later statement in the post-run block dies
    with it — after the run row has already been recorded as ok. Reproduced in
    review: a green sweep with zero errors, and the drafting, archive linking and
    notification all silently skipped.
    """
    from sqlalchemy import select, text
    from sqlalchemy.orm import sessionmaker

    from newspulse import job, themes
    from newspulse.db import make_engine
    from newspulse.models import Base, Client

    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    with factory() as session:
        session.add(Client(name="Kaputt AG", aliases=[], keywords=[], alert_topics=[]))
        session.commit()

        def _explode(session_, client, **kwargs):
            session_.execute(text("INSERT INTO clients (id) VALUES (1)"))

        monkeypatch.setattr(themes, "settle", _explode)
        job.run(session, analyzer=_NullAnalyzer(), feeds=[], fetch=lambda *a, **k: [])

        # The session survived: the sweep could still read and write after it.
        assert session.scalars(select(Client)).all()
