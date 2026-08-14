"""The one-off broom for radar hits that were never this mandate's field."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.orm import sessionmaker

from newspulse import radar_cleanup
from newspulse.db import make_engine
from newspulse.models import Article, Base, Client, TopicHit

_NOW = dt.datetime(2026, 8, 15, 9, 0, tzinfo=dt.UTC)


@pytest.fixture
def session():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as sess:
        yield sess


def _hit(session, client, title, source="Cointelegraph"):
    article = Article(
        title=title, url=f"https://ex.de/{abs(hash(title)) % 10**7}", source=source,
        published_at=_NOW - dt.timedelta(days=2), fetched_at=_NOW,
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


def test_applying_removes_only_the_off_theme_rows(session):
    client = _client(session)
    keep = _hit(session, client, "Firmenkunden-Banking wird zum Preiskampf", "Handelsblatt")
    _hit(session, client, "Putin Signs Russia's First Crypto Law")

    removed = radar_cleanup.clean(session, apply=True)

    assert [r.headline for r in removed] == ["Putin Signs Russia's First Crypto Law"]
    remaining = session.query(TopicHit).all()
    assert [row.article_id for row in remaining] == [keep.id]


def test_the_article_itself_survives(session):
    """It is another mandate's market, not rubbish. Only the link is cut."""
    client = _client(session)
    stray = _hit(session, client, "Putin Signs Russia's First Crypto Law")

    radar_cleanup.clean(session, apply=True)

    assert session.get(Article, stray.id) is not None


def test_a_mandate_without_themes_is_left_alone(session):
    """No themes, nothing to judge against — and no radar either. Deleting on a
    standard that cannot be evaluated is guessing."""
    client = _client(session, name="Ohne Themen", keywords=())
    _hit(session, client, "Irgendeine Meldung")

    assert radar_cleanup.survey(session) == []
