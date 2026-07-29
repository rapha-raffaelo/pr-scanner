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


# --- send_digest, the scheduled path ----------------------------------------
#
# Regression: `newspulse run && newspulse digest` crashed on the server with
# TypeError — SmtpConfig.from_env() was called with no argument. Nothing caught
# it because no test exercised send_digest at all; every digest test built the
# body directly. The failure landed on the one path that has to be
# unbreakable, since it runs unattended from cron.


def test_send_digest_without_smtp_configured_returns_none_not_raises(factory, monkeypatch):
    """An unconfigured mailbox is a no-op, never an exception.

    The digest is a convenience bolted onto the end of the nightly run. If it
    can raise, it takes the whole scheduled job down with it and the failure
    surfaces as a broken sweep rather than an unsent mail.
    """
    from newspulse import digest as digest_mod

    for var in ("NEWSPULSE_SMTP_HOST", "NEWSPULSE_SMTP_RECIPIENT"):
        monkeypatch.delenv(var, raising=False)

    with factory() as session:
        assert digest_mod.send_digest(session) is None


def test_send_digest_reads_smtp_settings_from_the_process_environment(factory, monkeypatch):
    """With SMTP set in the environment it resolves a config and sends.

    Guards the actual call signature: passing the environment is what the
    crash was about, so asserting delivery happens proves it end to end.
    """
    from newspulse import digest as digest_mod

    monkeypatch.setenv("NEWSPULSE_SMTP_HOST", "smtp.example.de")
    monkeypatch.setenv("NEWSPULSE_SMTP_RECIPIENT", "lucas@example.de")

    sent: list = []
    with factory() as session:
        result = digest_mod.send_digest(
            session, send=lambda summary, cfg: sent.append((summary, cfg))
        )

    assert result is not None
    assert len(sent) == 1
    assert sent[0][1].host == "smtp.example.de"
    assert sent[0][1].recipient == "lucas@example.de"


def test_send_digest_swallows_delivery_failure(factory, monkeypatch):
    """A dead mail server must not fail the run either."""
    from newspulse import digest as digest_mod

    monkeypatch.setenv("NEWSPULSE_SMTP_HOST", "smtp.example.de")
    monkeypatch.setenv("NEWSPULSE_SMTP_RECIPIENT", "lucas@example.de")

    def _boom(summary, cfg):
        raise OSError("connection refused")

    with factory() as session:
        assert digest_mod.send_digest(session, send=_boom) is None
