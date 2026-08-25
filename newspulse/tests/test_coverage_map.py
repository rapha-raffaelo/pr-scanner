"""The Coverage Map page when there is nothing to plot.

The chart itself is covered by ``test_market.py``. This is about the state it
falls into when the data does not support it, which used to be no state at all:
an empty white box with a legend across it.
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


def _covered(session, client: Client, source: str, n: int) -> None:
    for i in range(n):
        article = Article(
            title=f"{client.name} in {source} {i}",
            url=f"https://ex.de/{client.name}-{source}-{i}",
            source=source,
            published_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=2),
            fetched_at=dt.datetime.now(dt.UTC),
            summary_text=None,
            language="de",
            title_hash=f"{abs(hash((client.name, source, i))):016d}"[:16],
        )
        session.add(article)
        session.flush()
        session.add(
            Analysis(
                article_id=article.id, client_id=client.id, summary="s",
                category=Category.SONSTIGES, relevance_score=5,
                importance_score=5, is_alert=False,
            )
        )


def test_a_map_without_a_peer_group_says_so_instead_of_drawing_nothing(factory, client):
    """"Auch die Coverage Map bei IB-7 ist komplett leer."

    A map compares two sides. With no competitor linked there is no left-hand side
    to draw, and the page rendered the frame anyway — which reads as a broken
    feature rather than as a missing input.
    """
    with factory() as session:
        subject = Client(name="IB-7", aliases=[], keywords=[], alert_topics=[])
        session.add(subject)
        session.flush()
        _covered(session, subject, "Kosmetik-Journal", 1)
        session.commit()
        subject_id = subject.id

    body = " ".join(client.get(f"/client/{subject_id}/map").text.split())

    assert "Noch kein Wettbewerber hinterlegt" in body
    assert "Vergleichsgruppe festlegen" in body
    assert 'class="diverge"' not in body


def test_with_a_peer_group_but_no_volume_it_blames_the_window(factory, client):
    """The other cause, and it needs the opposite response: the comparison is set
    up correctly and there simply is not enough coverage yet."""
    with factory() as session:
        subject = Client(name="IB-7", aliases=[], keywords=[], alert_topics=[])
        rival = Client(
            name="Beauty Rival", aliases=[], keywords=[], alert_topics=[],
            is_competitor=True,
        )
        session.add_all([subject, rival])
        session.flush()
        subject.competitors.append(rival)
        _covered(session, subject, "Kosmetik-Journal", 1)
        session.commit()
        subject_id = subject.id

    body = " ".join(client.get(f"/client/{subject_id}/map").text.split())

    assert "Zu wenig Berichterstattung im Zeitraum" in body
    assert "Noch kein Wettbewerber hinterlegt" not in body


def test_a_map_with_a_real_imbalance_still_draws(factory, client):
    """The guard must not swallow the chart it is protecting."""
    with factory() as session:
        subject = Client(name="IB-7", aliases=[], keywords=[], alert_topics=[])
        rival = Client(
            name="Beauty Rival", aliases=[], keywords=[], alert_topics=[],
            is_competitor=True,
        )
        session.add_all([subject, rival])
        session.flush()
        subject.competitors.append(rival)
        _covered(session, rival, "Kosmetik-Journal", 4)
        session.commit()
        subject_id = subject.id

    body = client.get(f"/client/{subject_id}/map").text

    assert 'class="diverge"' in body
    assert "Kosmetik-Journal" in body


def test_without_a_peer_group_a_busy_mandate_gets_a_ranking_not_half_a_chart(
    factory, client
):
    """Freedom24: twenty-four outlets, every bar on the right, the left half blank.

    The case above has nothing to plot at all. This one has plenty — it simply
    has nothing to plot on the *left*, and the diverging form then makes four
    promises it cannot keep: a legend for a series with no data, a lead about
    gaps where there are none, a sort by imbalance over a column of zeroes, and
    half the width given to an empty side. The rows are still worth reading. They
    are a ranking of who covers this mandate, so the page says that instead.
    """
    with factory() as session:
        subject = Client(name="Freedom24", aliases=[], keywords=[], alert_topics=[])
        session.add(subject)
        session.flush()
        _covered(session, subject, "Manager Magazin", 6)
        _covered(session, subject, "Capital", 2)
        session.commit()
        subject_id = subject.id

    body = client.get(f"/client/{subject_id}/map").text
    flat = " ".join(body.split())

    assert "diverge--solo" in body
    assert "Wer über Freedom24 schreibt" in flat
    assert "nach Menge sortiert" in flat
    # Neither half of what cannot be drawn is advertised.
    assert "diverge__legend" not in body
    assert "nach Ungleichgewicht sortiert" not in flat
    # Busiest first — the ranking's only claim, and the reverse of the order the
    # imbalance sort produces when every row leans the same way.
    assert flat.index("Manager Magazin") < flat.index("Capital")


def test_a_peer_group_still_gets_the_two_sided_chart(factory, client):
    """The collapse must not swallow the comparison it stands in for."""
    with factory() as session:
        subject = Client(name="IB-7", aliases=[], keywords=[], alert_topics=[])
        rival = Client(
            name="Beauty Rival", aliases=[], keywords=[], alert_topics=[],
            is_competitor=True,
        )
        session.add_all([subject, rival])
        session.flush()
        subject.competitors.append(rival)
        _covered(session, rival, "Kosmetik-Journal", 4)
        _covered(session, subject, "Kosmetik-Journal", 1)
        session.commit()
        subject_id = subject.id

    body = client.get(f"/client/{subject_id}/map").text

    assert "diverge--solo" not in body
    assert "diverge__legend" in body
