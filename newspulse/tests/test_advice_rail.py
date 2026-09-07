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


# --- Declining an occasion -------------------------------------------------------
#
# "hier wäre es gut wenn du ein Update machst wo kreuze zum schliessen und
# verwerfen der vorschläge sind. also mit einem doppelten bestätigugngs fesnter"
#
# The rail is a row of proposals. One a consultant has looked at and rejected
# must leave the row, and it must take two clicks to make it leave: the card it
# removes is the one the reader is looking at.


def test_an_occasion_card_carries_the_cross_and_its_question(web, factory):
    client_id, older, newer = _mandate_with_two_occasions(factory)

    body = web.get(f"/client/{client_id}/advice").text

    assert f"/client/{client_id}/impulse/{newer}/dismiss" in body
    assert "Sind Sie sicher, dass Sie diesen Vorschlag verwerfen wollen?" in body
    # Both answers, and the one that acts is a form: no script between the
    # reader and the thing that removes a row.
    assert "Ja, verwerfen" in body and "Abbrechen" in body


def test_a_month_carries_no_cross(web, factory):
    """A period is not a proposal anyone declines; it is a month that ended."""
    from newspulse.models import Report

    client_id, _older, _newer = _mandate_with_two_occasions(factory)
    with factory() as session:
        session.add(
            Report(
                client_id=client_id,
                period_start=dt.datetime(2026, 7, 1, tzinfo=dt.UTC),
                period_end=dt.datetime(2026, 8, 1, tzinfo=dt.UTC),
                generated_at=dt.datetime(2026, 8, 3, tzinfo=dt.UTC),
            )
        )
        session.commit()

    body = web.get(f"/client/{client_id}/advice").text

    zeitraum = body.split('class="rail__k">Zeitraum', 1)[1].split("</div>", 1)[0]
    assert "/dismiss" not in zeitraum


def test_declining_removes_the_occasion_from_the_rail_and_the_page(web, factory):
    client_id, older, newer = _mandate_with_two_occasions(factory)

    answer = web.post(
        f"/client/{client_id}/impulse/{newer}/dismiss", follow_redirects=False
    )
    body = web.get(f"/client/{client_id}/advice").text

    assert answer.status_code == 303
    assert "Der neuere Anlass" not in body
    assert "Der ältere Anlass" in body, "the other one stays"


def test_declining_the_card_on_screen_lands_on_the_next_one(web, factory):
    """The redirect names no entry, so the page falls back to the newest that
    still stands — never to the one just hidden, and never to an empty page
    while another occasion exists."""
    client_id, older, newer = _mandate_with_two_occasions(factory)

    answer = web.post(
        f"/client/{client_id}/impulse/{newer}/dismiss", follow_redirects=False
    )
    body = web.get(answer.headers["location"]).text

    assert body.count('<article class="impulse"') == 1
    assert "Der ältere Anlass" in body


def test_declining_is_a_mark_and_the_row_stays_on_record(factory, web):
    """Marked, never deleted: texts written from the occasion hang on it."""
    client_id, older, newer = _mandate_with_two_occasions(factory)

    web.post(f"/client/{client_id}/impulse/{newer}/dismiss", follow_redirects=False)

    with factory() as session:
        row = session.get(Angle, newer)
        assert row is not None
        assert row.dismissed_at is not None


def test_a_guessed_id_cannot_decline_another_mandates_occasion(web, factory):
    client_id, older, newer = _mandate_with_two_occasions(factory)
    with factory() as session:
        other = Client(name="Beta", aliases=[], keywords=[], alert_topics=[])
        session.add(other)
        session.commit()
        other_id = other.id

    answer = web.post(
        f"/client/{other_id}/impulse/{newer}/dismiss", follow_redirects=False
    )

    assert answer.status_code == 404
    with factory() as session:
        assert session.get(Angle, newer).dismissed_at is None


def test_the_client_report_still_counts_a_declined_draft(factory):
    """A draft written in a period was written in it. A KPI that shrinks when
    somebody tidies a list is not a measurement."""
    from newspulse import angles

    client_id, older, newer = _mandate_with_two_occasions(factory)
    with factory() as session:
        session.get(Angle, newer).dismissed_at = dt.datetime.now(dt.UTC)
        session.commit()

        shown = angles.for_client(session, client_id, limit=None)
        counted = angles.for_client(
            session, client_id, limit=None, include_dismissed=True
        )

    assert [a.id for a in shown] == [older]
    assert {a.id for a in counted} == {older, newer}
