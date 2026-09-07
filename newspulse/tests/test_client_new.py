"""The mask for creating a mandate.

"auch haben wir jetzt 'neuer mandant' als button aber das linked direkt zu den
Einstellungen nicht aber zu einer Maske die uns hilft daten zum neuen Mandanten
einzugeben."

The button pointed at a collapsed ``<details>`` in Settings — a settings row,
not a way in. Three fields now, because onboarding does the rest: it settles the
industry, proposes and measures the themes, reads the profile off the website,
fetches the archive and drafts the first impulse. Asking for search terms and
alert topics here would be asking the consultant to do that work himself, before
he has seen a single article.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse.models import Base, Client
from newspulse.web.app import create_app, get_db


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def web(factory):
    app = create_app()

    def _db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _db
    return TestClient(app)


def test_the_mask_asks_for_what_the_tool_cannot_find_out(web):
    body = web.get("/mandant/neu").text

    assert 'name="name"' in body
    assert 'name="website"' in body
    assert 'name="country"' in body
    # And not for the four things onboarding settles itself.
    for asked_before in ('name="industry"', 'name="keywords"', 'name="alert_topics"', 'name="aliases"'):
        assert asked_before not in body, f"{asked_before} is onboarding's job now"


def test_the_mask_says_what_will_happen_next(web):
    """The answer to "what am I signing up for", which a settings row could
    never give."""
    body = web.get("/mandant/neu").text

    assert "Was danach von selbst passiert" in body
    assert "Das Profil wird von der Website gelesen" in body


def test_the_buttons_point_at_the_mask_and_not_at_settings(web):
    sidebar = web.get("/").text

    assert '/mandant/neu' in sidebar
    assert "/settings#neuer-mandant" not in sidebar


def test_creating_lands_on_the_mandate_it_created(web, factory, monkeypatch):
    """The redirect is what makes this a way in rather than a form: the
    consultant arrives where the onboarding he started is visibly running."""
    from newspulse.web.routes import settings as settings_routes

    monkeypatch.setattr(settings_routes, "_start_onboarding", lambda cid, name: None)

    answer = web.post(
        "/mandant/neu",
        data={"name": "Neu AG", "website": "neu-ag.de", "country": "DE"},
        follow_redirects=False,
    )

    with factory() as session:
        created = session.scalars(select(Client).where(Client.name == "Neu AG")).one()
        assert created.website == "https://neu-ag.de"
    assert answer.status_code == 303
    assert answer.headers["location"] == f"/client/{created.id}/heute"


def test_creating_starts_the_onboarding(web, monkeypatch):
    from newspulse.web.routes import settings as settings_routes

    started: list[str] = []
    monkeypatch.setattr(
        settings_routes, "_start_onboarding", lambda cid, name: started.append(name)
    )

    web.post("/mandant/neu", data={"name": "Neu AG"}, follow_redirects=False)

    assert started == ["Neu AG"]


def test_a_refused_name_comes_back_with_what_was_typed(web, factory, monkeypatch):
    """A duplicate must not cost the consultant the two fields he did fill."""
    from newspulse.web.routes import settings as settings_routes

    monkeypatch.setattr(settings_routes, "_start_onboarding", lambda cid, name: None)
    web.post("/mandant/neu", data={"name": "Neu AG"}, follow_redirects=False)

    answer = web.post(
        "/mandant/neu",
        data={"name": "Neu AG", "website": "neu-ag.de", "country": "AT"},
        follow_redirects=False,
    )

    assert answer.status_code == 200, "re-rendered, not redirected"
    assert 'value="Neu AG"' in answer.text
    assert 'value="neu-ag.de"' in answer.text
    assert 'value="AT"' in answer.text
