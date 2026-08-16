"""The one-off broom for radar hits that were never this mandate's field."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import radar_cleanup
from newspulse.models import Article, Base, Client, TopicHit

_NOW = dt.datetime(2026, 8, 15, 9, 0, tzinfo=dt.UTC)


@pytest.fixture
def session():
    # StaticPool, because the TestClient below serves the request on another
    # thread: a plain :memory: engine hands that thread its own empty database and
    # the route fails with "no such table: clients".
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as sess:
        yield sess


def _hit(session, client, title, source="Cointelegraph", *, days=2):
    article = Article(
        title=title, url=f"https://ex.de/{abs(hash(title)) % 10**7}", source=source,
        published_at=_NOW - dt.timedelta(days=days), fetched_at=_NOW,
        summary_text=None, language="de", title_hash=f"{abs(hash(title)):016d}"[:16],
    )
    session.add(article)
    session.flush()
    session.add(TopicHit(client_id=client.id, article_id=article.id, found_at=_NOW))
    session.commit()
    return article


def _client(session, name="Qonto", keywords=("Firmenkunden-Banking",)):
    client = Client(name=name, aliases=[], keywords=list(keywords), alert_topics=[])
    session.add(client)
    session.commit()
    return client


def test_a_survey_changes_nothing(session):
    """The default has to be safe: this deletes rows on a live database."""
    client = _client(session)
    _hit(session, client, "Strategy sells 1,638 Bitcoin to fund dividends")

    found = radar_cleanup.survey(session)

    assert len(found) == 1
    assert session.query(TopicHit).count() == 1


def test_removing_takes_exactly_the_pairs_it_is_given(session):
    """Not "whatever a fresh survey finds": the page renders a bounded list and
    the operator is told to read it, so the button must delete that list and not
    a second query's answer."""
    client = _client(session)
    keep = _hit(session, client, "Firmenkunden-Banking wird zum Preiskampf", "Handelsblatt")
    stray = _hit(session, client, "Putin Signs Russia's First Crypto Law")

    removed = radar_cleanup.remove(session, [(client.id, stray.id)])

    assert removed == 1
    remaining = session.query(TopicHit).all()
    assert [row.article_id for row in remaining] == [keep.id]


def test_removing_nothing_is_not_an_error(session):
    """An empty form posts no rows at all."""
    client = _client(session)
    _hit(session, client, "Putin Signs Russia's First Crypto Law")

    assert radar_cleanup.remove(session, []) == 0
    assert session.query(TopicHit).count() == 1


def test_a_hit_outside_the_window_is_left_alone(session):
    """A mandate given a radar last night would otherwise have its whole archive
    judged against four terms chosen the same night."""
    client = _client(session)
    _hit(session, client, "Uraltes Krypto-Thema", days=200)

    assert radar_cleanup.survey(session) == []


def test_the_article_itself_survives(session):
    """It is another mandate's market, not rubbish. Only the link is cut."""
    client = _client(session)
    stray = _hit(session, client, "Putin Signs Russia's First Crypto Law")

    radar_cleanup.remove(session, [(client.id, stray.id)])

    assert session.get(Article, stray.id) is not None


def test_a_mandate_without_themes_is_left_alone(session):
    """No themes, nothing to judge against — and no radar either. Deleting on a
    standard that cannot be evaluated is guessing."""
    client = _client(session, name="Ohne Themen", keywords=())
    _hit(session, client, "Irgendeine Meldung")

    assert radar_cleanup.survey(session) == []


# --- The survey and its button ---------------------------------------------------


@pytest.fixture
def web(session):
    from fastapi.testclient import TestClient

    from newspulse.web.app import create_app, get_db

    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    return TestClient(app)


def test_the_settings_page_does_not_survey_unless_asked(session, web):
    """It walks every stored hit, and the page is opened many times a day for
    reasons that have nothing to do with this."""
    client = _client(session)
    _hit(session, client, "Putin Signs Russia First Crypto Law")

    body = web.get("/settings").text

    assert "Radar-Treffer prüfen" in body
    assert "Putin Signs" not in body


def test_the_survey_shows_what_would_go_before_anything_goes(session, web):
    client = _client(session)
    _hit(session, client, "Putin Signs Russia First Crypto Law")

    body = web.get("/settings?radar=1").text

    assert "Putin Signs" in body
    assert "Cointelegraph" in body
    assert "Zuordnungen entfernen" in body
    assert session.query(TopicHit).count() == 1, "looking is not deleting"


def test_a_clean_portfolio_says_so(session, web):
    client = _client(session)
    _hit(session, client, "Firmenkunden-Banking wird zum Preiskampf", "Handelsblatt")

    body = web.get("/settings?radar=1").text

    assert "Alle gespeicherten Radar-Treffer tragen ein Thema" in body


def test_the_button_removes_them_and_comes_back_to_the_survey(session, web):
    client = _client(session)
    _hit(session, client, "Putin Signs Russia First Crypto Law")

    stray = session.query(TopicHit).one()
    resp = web.post(
        "/settings/radar/cleanup",
        data={"hit": f"{stray.client_id}:{stray.article_id}"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings?radar=1"
    assert session.query(TopicHit).count() == 0


def test_the_button_only_removes_what_the_page_carried(session, web):
    """The row the sweep added between reading and pressing is not in the form,
    so it is not deleted."""
    client = _client(session)
    shown = _hit(session, client, "Putin Signs Russia's First Crypto Law")
    arrived_later = _hit(session, client, "Visa Widens Stablecoin Payouts")

    web.post("/settings/radar/cleanup",
             data={"hit": f"{client.id}:{shown.id}"}, follow_redirects=False)

    remaining = [row.article_id for row in session.query(TopicHit).all()]
    assert remaining == [arrived_later.id]


def test_a_malformed_row_is_ignored_rather_than_crashing(session, web):
    client = _client(session)
    _hit(session, client, "Putin Signs Russia's First Crypto Law")

    resp = web.post("/settings/radar/cleanup",
                    data={"hit": ["nonsense", "1:", ":2"]}, follow_redirects=False)

    assert resp.status_code == 303
    assert session.query(TopicHit).count() == 1

