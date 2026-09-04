"""Route tests for the portfolio-wide Archiv view (``GET /archive``).

Drives the route through FastAPI's TestClient against a seeded in-memory
database. The point of this view is that it spans *all* clients, so the tests
seed more than one and assert the filters compose across them.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse.models import Analysis, Article, Base, Category, Client
from newspulse.web.app import create_app, get_db


def _local_noon(day: dt.date) -> dt.datetime:
    tz = dt.datetime.now().astimezone().tzinfo
    return dt.datetime.combine(day, dt.time(12, 0), tzinfo=tz)


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def client(factory):
    app = create_app()

    def _override_get_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


def _seed(session, *, client_obj, title, url, source, day, category, importance=5,
          relevance=5, is_alert=False, summary="Zusammenfassung."):
    article = Article(
        title=title, url=url, source=source,
        published_at=_local_noon(day), fetched_at=_local_noon(day),
        summary_text="Snippet.", language="de", title_hash=url[-10:],
    )
    session.add(article)
    session.flush()
    session.add(
        Analysis(
            article_id=article.id, client_id=client_obj.id, summary=summary,
            category=category, is_relevant=relevance >= 1, relevance_score=relevance,
            importance_score=importance, is_alert=is_alert,
        )
    )


@pytest.fixture
def portfolio(factory):
    """Two clients, two months, two publishers, two categories."""
    with factory() as s:
        alpha = Client(name="Alpha AG")
        beta = Client(name="Beta AG")
        s.add_all([alpha, beta])
        s.flush()
        _seed(s, client_obj=alpha, title="ALPHA-JUNI-FAZ Werk schliesst",
              url="https://ex.de/a1", source="FAZ", day=dt.date(2026, 6, 15),
              category=Category.KRISE, importance=9, is_alert=True)
        _seed(s, client_obj=alpha, title="ALPHA-JULI-SZ Zahlen vorgelegt",
              url="https://ex.de/a2", source="SZ", day=dt.date(2026, 7, 10),
              category=Category.FINANZEN)
        _seed(s, client_obj=beta, title="BETA-JULI-FAZ Neuer Vorstand",
              url="https://ex.de/b1", source="FAZ", day=dt.date(2026, 7, 20),
              category=Category.PERSONALIE)
        s.commit()
    return factory


def test_archive_spans_every_client_by_default(client, portfolio):
    body = client.get("/archive").text
    assert "ALPHA-JUNI-FAZ" in body
    assert "ALPHA-JULI-SZ" in body
    assert "BETA-JULI-FAZ" in body
    assert "3 Artikel" in body


def test_filter_by_client(client, portfolio, factory):
    with factory() as s:
        beta_id = s.query(Client).filter_by(name="Beta AG").one().id
    body = client.get("/archive", params={"client": beta_id}).text
    assert "BETA-JULI-FAZ" in body
    assert "ALPHA-JUNI-FAZ" not in body


def test_filter_by_month(client, portfolio):
    body = client.get("/archive", params={"month": "2026-06"}).text
    assert "ALPHA-JUNI-FAZ" in body
    assert "ALPHA-JULI-SZ" not in body
    assert "BETA-JULI-FAZ" not in body


def test_filter_by_publisher(client, portfolio):
    body = client.get("/archive", params={"source": "FAZ"}).text
    assert "ALPHA-JUNI-FAZ" in body
    assert "BETA-JULI-FAZ" in body
    assert "ALPHA-JULI-SZ" not in body


def test_filters_compose_as_and_terms(client, portfolio):
    """Publisher AND month together narrow to the single matching row."""
    body = client.get("/archive", params={"source": "FAZ", "month": "2026-07"}).text
    assert "BETA-JULI-FAZ" in body
    assert "ALPHA-JUNI-FAZ" not in body  # right publisher, wrong month
    assert "ALPHA-JULI-SZ" not in body   # right month, wrong publisher


def test_free_text_search_matches_headline(client, portfolio):
    body = client.get("/archive", params={"q": "Vorstand"}).text
    assert "BETA-JULI-FAZ" in body
    assert "ALPHA-JUNI-FAZ" not in body


def test_month_dropdown_lists_every_month_with_coverage(client, portfolio):
    """Computed over the whole archive, so picking a month doesn't collapse the
    dropdown to that one option."""
    body = client.get("/archive", params={"month": "2026-07"}).text
    assert 'value="2026-06"' in body
    assert 'value="2026-07"' in body
    assert "Juni 2026" in body and "Juli 2026" in body


def test_unparseable_month_degrades_to_no_filter(client, portfolio):
    """A hand-edited URL must not 500 or silently empty the page."""
    resp = client.get("/archive", params={"month": "not-a-month"})
    assert resp.status_code == 200
    assert "3 Artikel" in resp.text


def test_irrelevant_analyses_are_excluded(client, factory):
    """relevance_score=0 means the story does not concern the client."""
    with factory() as s:
        c = Client(name="Alpha AG")
        s.add(c)
        s.flush()
        _seed(s, client_obj=c, title="NOISE Nicht relevant", url="https://ex.de/n",
              source="FAZ", day=dt.date(2026, 7, 1), category=Category.SONSTIGES,
              relevance=0)
        s.commit()
    body = client.get("/archive").text
    assert "NOISE Nicht relevant" not in body
    assert "Keine Artikel" in body


def test_empty_archive_renders_an_empty_state(client):
    resp = client.get("/archive")
    assert resp.status_code == 200
    assert "Keine Artikel" in resp.text


def test_archive_can_be_filtered_to_one_media_tier(client, factory):
    """A PR reader often wants only the Leitmedien, or only to see how much of a
    month was automated finance-ticker noise."""
    with factory() as s:
        c = Client(name="Alpha AG")
        s.add(c)
        s.flush()
        _seed(s, client_obj=c, title="LEIT Story", url="https://ex.de/1",
              source="FAZ", day=dt.date(2026, 7, 10), category=Category.KRISE)
        _seed(s, client_obj=c, title="TICKER Story", url="https://ex.de/2",
              source="Ad-hoc-news.de", day=dt.date(2026, 7, 11), category=Category.FINANZEN)
        _seed(s, client_obj=c, title="REGIO Story", url="https://ex.de/3",
              source="Oberberg-Aktuell", day=dt.date(2026, 7, 12), category=Category.SONSTIGES)
        s.commit()

    tier1 = client.get("/archive", params={"tier": "1"}).text
    assert "LEIT Story" in tier1
    assert "TICKER Story" not in tier1 and "REGIO Story" not in tier1

    tier3 = client.get("/archive", params={"tier": "3"}).text
    assert "TICKER Story" in tier3
    assert "LEIT Story" not in tier3

    # Unlisted outlets fall to the neutral middle tier.
    tier2 = client.get("/archive", params={"tier": "2"}).text
    assert "REGIO Story" in tier2


def test_an_unknown_tier_value_shows_everything(client, factory):
    with factory() as s:
        c = Client(name="Alpha AG")
        s.add(c)
        s.flush()
        _seed(s, client_obj=c, title="EINE Story", url="https://ex.de/1",
              source="FAZ", day=dt.date(2026, 7, 10), category=Category.KRISE)
        s.commit()
    assert "EINE Story" in client.get("/archive", params={"tier": "unfug"}).text


def test_a_competitor_is_not_offered_in_the_client_filter(client, factory):
    """A dropdown that offers a company whose results are filtered out is a
    filter that silently returns nothing — which reads as a bug, not a setting."""
    with factory() as s:
        s.add(Client(name="Alpha AG"))
        s.add(Client(name="Rivale AG", is_competitor=True))
        s.commit()

    body = client.get("/archive").text
    assert "Alpha AG" in body
    assert "Rivale AG" not in body

    # Opting in widens every control, not just the rows.
    widened = client.get("/archive", params={"with_competitors": "1"}).text
    assert "Rivale AG" in widened


def test_publisher_options_follow_the_same_scope(client, factory):
    with factory() as s:
        mandate = Client(name="Alpha AG")
        rival = Client(name="Rivale AG", is_competitor=True)
        s.add_all([mandate, rival])
        s.flush()
        _seed(s, client_obj=mandate, title="A", url="https://ex.de/a",
              source="Mandantenblatt", day=dt.date(2026, 7, 10), category=Category.KRISE)
        _seed(s, client_obj=rival, title="B", url="https://ex.de/b",
              source="Nurwettbewerb", day=dt.date(2026, 7, 11), category=Category.KRISE)
        s.commit()

    body = client.get("/archive").text
    assert "Mandantenblatt" in body
    assert "Nurwettbewerb" not in body
