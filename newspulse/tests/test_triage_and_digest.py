"""Workflow state and the morning digest."""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse.digest import build_digest
from newspulse.models import Analysis, Article, Base, Category, Client, TriageState
from newspulse.web.app import create_app, get_db


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


def _seed(session, name="Alpha AG", *, alert=False, competitor=False, title="Story"):
    c = session.query(Client).filter_by(name=name).one_or_none()
    if c is None:
        c = Client(name=name, is_competitor=competitor)
        session.add(c)
        session.flush()
    when = dt.datetime.now().astimezone()
    art = Article(
        title=title, url=f"https://ex.de/{title}", source="FAZ", published_at=when,
        fetched_at=when, summary_text="s", language="de", title_hash=title[:8],
    )
    session.add(art)
    session.flush()
    analysis = Analysis(
        article_id=art.id, client_id=c.id, summary="Zusammenfassung.",
        category=Category.KRISE, relevance_score=5, importance_score=8, is_alert=alert,
    )
    session.add(analysis)
    session.commit()
    return analysis


# --- Triage --------------------------------------------------------------------


def test_new_coverage_starts_unread(factory):
    with factory() as s:
        assert _seed(s).triage_state is TriageState.NEU


def test_marking_a_state_persists_it(factory, client):
    with factory() as s:
        analysis_id = _seed(s).id
    client.post(f"/triage/{analysis_id}", data={"state": "erledigt"}, follow_redirects=False)
    with factory() as s:
        assert s.get(Analysis, analysis_id).triage_state is TriageState.ERLEDIGT


def test_clicking_the_same_state_again_clears_it(factory, client):
    """One button both marks and un-marks; a mis-click must be undoable."""
    with factory() as s:
        analysis_id = _seed(s).id
    for _ in range(2):
        client.post(f"/triage/{analysis_id}", data={"state": "markiert"}, follow_redirects=False)
    with factory() as s:
        assert s.get(Analysis, analysis_id).triage_state is TriageState.NEU


def test_an_unknown_state_is_a_no_op_not_an_error(factory, client):
    with factory() as s:
        analysis_id = _seed(s).id
    resp = client.post(f"/triage/{analysis_id}", data={"state": "unfug"}, follow_redirects=False)
    assert resp.status_code == 303
    with factory() as s:
        assert s.get(Analysis, analysis_id).triage_state is TriageState.NEU


def test_a_stale_analysis_id_does_not_lose_the_page(client):
    resp = client.post("/triage/9999", data={"state": "gelesen"}, follow_redirects=False)
    assert resp.status_code == 303


def test_an_offsite_redirect_target_is_refused(factory, client):
    """redirect_to comes from a form field; an absolute URL would make this an
    open redirect."""
    with factory() as s:
        analysis_id = _seed(s).id
    resp = client.post(
        f"/triage/{analysis_id}",
        data={"state": "gelesen", "redirect_to": "https://evil.example/"},
        follow_redirects=False,
    )
    assert resp.headers["location"] == "/"


# --- Digest --------------------------------------------------------------------


def test_digest_counts_stories_and_alerts(factory):
    with factory() as s:
        _seed(s, title="Alpha schliesst Werk in Erfurt komplett", alert=True)
        _seed(s, title="Alpha eroeffnet Logistikzentrum in Polen")
        digest = build_digest(s)
    assert "2 Story(s)" in digest.subject
    assert "1 Alert(s)" in digest.subject
    assert "Alpha AG" in digest.body


def test_digest_excludes_competitors(factory):
    """A competitor is monitored, but the morning brief is about mandates."""
    with factory() as s:
        _seed(s, name="Alpha AG", title="Alpha meldet Rekordumsatz im Quartal")
        _seed(s, name="Rival AG", competitor=True, title="Rival meldet eigene Zahlen heute")
        digest = build_digest(s)
    assert "Alpha AG" in digest.body
    assert "Rival AG" not in digest.body


def test_quiet_day_still_produces_a_message(factory):
    """Absence of a digest should mean the run failed, not that it was quiet."""
    with factory() as s:
        digest = build_digest(s)
    assert "Keine Berichterstattung" in digest.body
    assert digest.total_stories == 0
