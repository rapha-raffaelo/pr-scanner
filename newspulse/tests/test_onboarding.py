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


# --- The wiring ----------------------------------------------------------------


def test_creating_a_client_starts_the_onboarding_fetch(monkeypatch):
    """The route must not wait for it: fetching and analysing takes minutes."""
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
    app = create_app()

    def _override():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override

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
