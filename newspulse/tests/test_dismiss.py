"""Taking an article out of a client's coverage.

The matcher favours recall on purpose — Claude decides afterwards — so a company
named after a fictional planet, or one with a namesake in another industry,
collects articles that are simply not about it. They sat in the archive, in the
counts and in the share of voice with no way to remove them.

The load-bearing test is the sweep below: dismissing marks the row rather than
deleting it, because a deleted analysis leaves the pair unanalysed and the next
sweep brings the article straight back.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import config
from newspulse.models import Analysis, Article, Base, Category, Client
from newspulse.web.app import create_app, get_db

_TEST_DAY = dt.date(2026, 7, 20)


def _noon(day: dt.date) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(12, 0), tzinfo=config.local_zone())


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
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


def _covered(session, *, title="Arrakis: Wüsten-Action auf der PS5", day=_TEST_DAY):
    client = Client(name="Arrakis", aliases=[], keywords=[], alert_topics=[])
    session.add(client)
    session.flush()
    article = Article(
        title=title,
        url=f"https://ex.de/{abs(hash(title)) % 100000}",
        source="PLAY3.DE",
        published_at=_noon(day),
        fetched_at=_noon(day),
        summary_text="Ein Satz.",
        language="de",
        title_hash=str(abs(hash(title)) % 10**8),
    )
    session.add(article)
    session.flush()
    analysis = Analysis(is_relevant=True, 
        article_id=article.id,
        client_id=client.id,
        summary="Handelt von Dune, nicht vom Mandanten.",
        category=Category.SONSTIGES,
        relevance_score=5,
        importance_score=5,
        is_alert=False,
    )
    session.add(analysis)
    session.commit()
    return client, article, analysis


def test_a_dismissed_article_leaves_every_view_at_once(factory, client):
    """One predicate, so there is no corner it survives in.

    There were nine copies of the relevance filter across as many modules; a
    second reason to hide a row would have had to find all of them.
    """
    with factory() as session:
        subject, _article, analysis = _covered(session)
        subject_id, analysis_id = subject.id, analysis.id

    day = _TEST_DAY.isoformat()
    assert "Wüsten-Action" in client.get("/today", params={"date": day}).text
    assert "Wüsten-Action" in client.get(f"/client/{subject_id}").text
    assert "Wüsten-Action" in client.get("/archive").text

    client.post(f"/coverage/{analysis_id}/dismiss", data={"redirect_to": "/"},
                follow_redirects=False)

    assert "Wüsten-Action" not in client.get("/today", params={"date": day}).text
    assert "Wüsten-Action" not in client.get(f"/client/{subject_id}").text
    assert "Wüsten-Action" not in client.get("/archive").text


def test_it_leaves_the_counts_and_the_share_of_voice(factory, client):
    """A wrong match in the archive is untidy; in a reported number it is worse."""
    with factory() as session:
        subject, _article, analysis = _covered(session)
        subject_id, analysis_id = subject.id, analysis.id

    # The archive count lives on the mandate's own page now: the portfolio card
    # answers "does this one need me today", which is a different question.
    # The archive count lives on the mandate's own page now: the portfolio card
    # answers "does this one need me today", which is a different question. Anchor
    # on the archive heading, not on a bare number the page header also carries.
    before = " ".join(client.get(f"/client/{subject_id}").text.split())
    assert "1 Artikel · Seite" in before
    client.post(f"/coverage/{analysis_id}/dismiss", follow_redirects=False)

    after = " ".join(client.get(f"/client/{subject_id}").text.split())
    assert "1 Artikel · Seite" not in after

    with factory() as session:
        from newspulse.reporting import share_of_voice

        voice = share_of_voice(session, session.get(Client, subject_id))
        assert voice[0].mentions == 0


def test_the_row_is_marked_not_deleted(factory, client):
    """Load-bearing. A deleted analysis leaves the pair unanalysed, so the next
    sweep re-matches it, analyses it again, and the article is back by morning —
    this row is what tells the sweep it has already been judged."""
    with factory() as session:
        _subject, _article, analysis = _covered(session)
        analysis_id = analysis.id

    client.post(f"/coverage/{analysis_id}/dismiss", follow_redirects=False)

    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Analysis)) == 1
        assert session.get(Analysis, analysis_id).dismissed_at is not None


def test_the_article_itself_survives(factory, client):
    """It is still a real article, and other clients may legitimately have it as
    coverage — only this pairing was wrong."""
    with factory() as session:
        _subject, _article, analysis = _covered(session)
        analysis_id = analysis.id

    client.post(f"/coverage/{analysis_id}/dismiss", follow_redirects=False)

    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Article)) == 1


def test_a_dismissal_can_be_undone(factory, client):
    """The scores were never touched, so there is nothing to rebuild."""
    with factory() as session:
        subject, _article, analysis = _covered(session)
        subject_id, analysis_id = subject.id, analysis.id

    client.post(f"/coverage/{analysis_id}/dismiss", follow_redirects=False)
    client.post(f"/coverage/{analysis_id}/restore", follow_redirects=False)

    assert "Wüsten-Action" in client.get(f"/client/{subject_id}").text


def test_the_prompts_stop_seeing_it_too(factory):
    """A dismissed article must not come back as evidence in a generated text."""
    with factory() as session:
        subject, _article, analysis = _covered(session)
        from newspulse import advisor

        assert len(advisor.recent_coverage(session, subject.id)) == 1
        analysis.dismissed_at = dt.datetime.now(dt.UTC)
        session.commit()
        assert advisor.recent_coverage(session, subject.id) == []


def test_the_digest_stops_counting_it(factory):
    with factory() as session:
        _subject, _article, analysis = _covered(session, day=dt.date.today())
        from newspulse.digest import build_digest

        assert build_digest(session).total_stories == 1
        analysis.dismissed_at = dt.datetime.now(dt.UTC)
        session.commit()
        assert build_digest(session).total_stories == 0


def test_dismissing_twice_keeps_the_first_date(factory, client):
    """The decision is dated; a second click must not silently re-date it."""
    with factory() as session:
        _subject, _article, analysis = _covered(session)
        analysis_id = analysis.id

    client.post(f"/coverage/{analysis_id}/dismiss", follow_redirects=False)
    with factory() as session:
        first = session.get(Analysis, analysis_id).dismissed_at

    client.post(f"/coverage/{analysis_id}/dismiss", follow_redirects=False)
    with factory() as session:
        assert session.get(Analysis, analysis_id).dismissed_at == first


def test_an_offsite_redirect_is_refused(factory, client):
    with factory() as session:
        _subject, _article, analysis = _covered(session)
        analysis_id = analysis.id

    resp = client.post(
        f"/coverage/{analysis_id}/dismiss",
        data={"redirect_to": "https://evil.example/"},
        follow_redirects=False,
    )

    assert resp.headers["location"] == "/"


def test_a_stale_id_is_a_no_op_not_an_error(client):
    """The button may have been on screen since before a re-run."""
    resp = client.post("/coverage/9999/dismiss", follow_redirects=False)
    assert resp.status_code == 303
