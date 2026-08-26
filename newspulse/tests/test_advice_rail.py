"""The Texte rail: one surface for the occasions and the months.

Impulse and Berichte were two tabs doing the same thing at two rhythms. They are
one now, and the rail is how an entry is chosen — which also fixes what made the
impulse page hard to read before the merge: four full cards, each with an idea,
seven formats and a send ledger, stacked down one page.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse.models import Angle, Base, Client
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

    def _override():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


def _mandate_with_two_occasions(factory) -> tuple[int, int, int]:
    with factory() as session:
        client = Client(name="Arrakis", aliases=[], keywords=[], alert_topics=[])
        session.add(client)
        session.flush()
        older = Angle(
            client_id=client.id, subject="Der ältere Anlass", message="Text A",
            context="", generated_at=dt.datetime(2026, 8, 20, 6, 10, tzinfo=dt.UTC),
        )
        newer = Angle(
            client_id=client.id, subject="Der neuere Anlass", message="Text B",
            context="", generated_at=dt.datetime(2026, 8, 25, 6, 10, tzinfo=dt.UTC),
        )
        session.add_all([older, newer])
        session.commit()
        return client.id, older.id, newer.id


def test_the_page_shows_one_occasion_and_links_the_rest(web, factory):
    """The newest by default, because that is what the reader came for."""
    client_id, older, newer = _mandate_with_two_occasions(factory)

    body = web.get(f"/client/{client_id}/advice").text

    assert body.count('<article class="impulse"') == 1
    assert "Der neuere Anlass" in body
    assert f"eintrag=anlass-{older}" in body, "the other one is one click away"


def test_the_rail_opens_the_occasion_it_names(web, factory):
    client_id, older, newer = _mandate_with_two_occasions(factory)

    body = web.get(f"/client/{client_id}/advice?eintrag=anlass-{older}").text

    assert body.count('<article class="impulse"') == 1
    assert "Der ältere Anlass" in body
    assert "Der neuere Anlass" in body, "still in the rail"
    card = body.split('<article class="impulse"', 1)[1]
    assert "Der neuere Anlass" not in card, "but not in the card"


def test_the_rail_carries_the_month_beside_the_occasions(web, factory):
    """The whole point of the merge: one row, both kinds of thing."""
    client_id, _older, _newer = _mandate_with_two_occasions(factory)

    rail = web.get(f"/client/{client_id}/advice").text
    rail = rail.split('<nav class="rail"', 1)[1].split("</nav>", 1)[0]

    assert "eintrag=anlass-" in rail
    assert f"/client/{client_id}/berichte?zeitraum=" in rail


def test_a_nonsense_entry_falls_back_to_the_newest(web, factory):
    """The value comes from a query string. A hand-edited one should land the
    reader somewhere real rather than on an empty page or a 422."""
    client_id, _older, newer = _mandate_with_two_occasions(factory)

    body = web.get(f"/client/{client_id}/advice?eintrag=anlass-999999").text

    assert body.count('<article class="impulse"') == 1
    assert "Der neuere Anlass" in body
