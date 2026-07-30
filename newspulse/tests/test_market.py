"""The Marktumfeld view: what the radar saw, and who reported it.

Radar articles were stored with nothing linking them to the client whose themes
found them, so the material was in the database and attached to nobody —
unbrowsable, and unusable for ranking outlets. ``topic_hits`` carries that pairing,
and this view is the first thing that reads it.

The load-bearing distinction throughout: an article that never names the client is
market material, not coverage of the client. It must appear here and nowhere that
counts a mandate's own press.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse.models import (
    Analysis,
    Article,
    Base,
    Category,
    Client,
    TopicHit,
)
from newspulse.web.app import create_app, get_db

_NOW = dt.datetime(2026, 7, 30, 9, 0, tzinfo=dt.UTC)


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

    def _override():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


def _client(session, name="Arrakis Finance", **over) -> Client:
    obj = Client(
        name=name,
        aliases=[],
        keywords=over.get("keywords", ["Onchain-Liquidität"]),
        alert_topics=over.get("alert_topics", []),
        country="DE",
    )
    session.add(obj)
    session.commit()
    return obj


def _article(session, title, *, source="yellow.com", author=None, age_days=1) -> Article:
    article = Article(
        title=title,
        url=f"https://ex.de/{abs(hash(title)) % 100000}",
        source=source,
        author=author,
        published_at=_NOW - dt.timedelta(days=age_days),
        fetched_at=_NOW,
        summary_text="Ein Satz.",
        language="de",
        title_hash=str(abs(hash(title)) % 10**8),
    )
    session.add(article)
    session.commit()
    return article


def _market(session, client_obj, article) -> None:
    session.add(
        TopicHit(article_id=article.id, client_id=client_obj.id, found_at=_NOW)
    )
    session.commit()


def _coverage(session, client_obj, article) -> None:
    session.add(
        Analysis(
            article_id=article.id,
            client_id=client_obj.id,
            summary="s",
            category=Category.PRODUKT,
            relevance_score=6,
            importance_score=6,
            is_alert=False,
        )
    )
    session.commit()


def test_the_market_view_lists_what_the_radar_found(factory, client):
    with factory() as session:
        subject = _client(session)
        _market(session, subject, _article(session, "BitMart schliesst nach neun Jahren"))
        _market(session, subject, _article(session, "BitMEX stellt Betrieb ein"))
        subject_id = subject.id

    body = client.get(f"/client/{subject_id}/market").text

    assert "BitMart schliesst nach neun Jahren" in body
    assert "BitMEX stellt Betrieb ein" in body


def test_market_material_stays_out_of_the_clients_own_archive(factory, client):
    """The distinction the whole second table exists for.

    A story that never names the mandate must not inflate the number the agency is
    judged on, so it appears in the market view and nowhere else.
    """
    with factory() as session:
        subject = _client(session)
        _market(session, subject, _article(session, "BitMEX stellt Betrieb ein"))
        subject_id = subject.id

    archive = client.get(f"/client/{subject_id}").text
    market = client.get(f"/client/{subject_id}/market").text

    assert "BitMEX stellt Betrieb ein" not in archive
    assert "BitMEX stellt Betrieb ein" in market


def test_outlets_on_the_subject_are_ranked_by_how_much_they_cover_it(factory, client):
    """The answer to "who should we pitch": whoever writes about the subject."""
    with factory() as session:
        subject = _client(session)
        for i in range(3):
            _market(session, subject, _article(session, f"Krypto {i}", source="CoinDesk"))
        _market(session, subject, _article(session, "Einzeln", source="yellow.com"))
        subject_id = subject.id

    body = client.get(f"/client/{subject_id}/market").text
    ranking = body.split("Medien im Themenfeld", 1)[1]

    assert ranking.index("CoinDesk") < ranking.index("yellow.com")


def test_outlets_on_the_client_are_a_separate_list(factory, client):
    """Existing relationships and targets mean different things to a consultant,
    so the two counts are never added together."""
    with factory() as session:
        subject = _client(session)
        _coverage(session, subject, _article(session, "Arrakis meldet Zahlen", source="Handelsblatt"))
        _market(session, subject, _article(session, "Markt bewegt sich", source="CoinDesk"))
        subject_id = subject.id

    body = client.get(f"/client/{subject_id}/market").text
    subject_block = body.split("Medien im Themenfeld", 1)[1].split("Medien über den Mandanten", 1)[0]
    client_block = body.split("Medien über den Mandanten", 1)[1]

    assert "CoinDesk" in subject_block
    assert "Handelsblatt" not in subject_block
    assert "Handelsblatt" in client_block


def test_the_journalist_list_says_when_the_feeds_carried_no_author(factory, client):
    """Measured on the live archive: 22 of 291 articles carry an author, and Google
    News — every radar hit — carries none. A padded list would be worse than an
    empty one, because pitching someone who does not cover the beat costs a
    relationship."""
    with factory() as session:
        subject = _client(session)
        _market(session, subject, _article(session, "Ohne Autor", author=None))
        subject_id = subject.id

    body = client.get(f"/client/{subject_id}/market").text

    assert "Kein Feed in diesem Zeitraum hat einen Autor mitgeliefert." in body


def test_a_journalist_is_listed_when_the_feed_did_supply_one(factory, client):
    with factory() as session:
        subject = _client(session)
        _market(session, subject, _article(session, "Mit Autor", source="heise online",
                                           author="Frank Schräer"))
        subject_id = subject.id

    body = client.get(f"/client/{subject_id}/market").text

    assert "Frank Schräer" in body


def test_a_client_without_themes_is_told_so_rather_than_shown_a_quiet_market(factory, client):
    """Without themes there is no radar, so an empty page here is a configuration
    fact — blaming the market for it would send the reader looking in the wrong
    place."""
    with factory() as session:
        subject = _client(session, keywords=[], alert_topics=[])
        subject_id = subject.id

    body = client.get(f"/client/{subject_id}/market").text

    assert "Für diesen Mandanten ist kein Themen-Radar eingerichtet." in body
    assert f'href="/settings?edit={subject_id}"' in body


def test_themes_are_shown_so_the_reader_knows_what_was_searched(factory, client):
    with factory() as session:
        subject = _client(session, keywords=["Onchain-Liquidität"], alert_topics=["Börsenschließung"])
        subject_id = subject.id

    body = client.get(f"/client/{subject_id}/market").text

    assert "Onchain-Liquidität" in body
    assert "Börsenschließung" in body


def test_the_tab_is_reachable_from_the_other_client_views(factory, client):
    with factory() as session:
        subject = _client(session)
        subject_id = subject.id

    for path in (f"/client/{subject_id}", f"/client/{subject_id}/map"):
        assert f"/client/{subject_id}/market" in client.get(path).text, path


def test_an_unknown_client_is_a_404(client):
    assert client.get("/client/9999/market").status_code == 404
