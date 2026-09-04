"""The noise a mandate never wants to see again — remembered.

A listed retailer collects three near-identical share-price items a day ("die
Aktie zeigt Stabilität", "klettert deutlich", "bleibt stabil"), each scored 4-5
out of 10 and therefore filed beside a real event like a regulator's reprimand.
The category dropdown could hide them, but it forgot the choice on every page
load. A PR consultant's verdict was the sharpest line in her review: that is the
point where a sixty-second triage stops being sixty seconds, and where she would
start opening the tab at noon instead of at nine.

Muting hides, it never discards. The archive, the counts and the export keep
everything — a number that silently changed with a reading preference would be a
worse problem than the one being solved.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import config
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

    def _override_get_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


def _local_noon() -> dt.datetime:
    """Midday on the day the page will show, in UTC.

    Not "a few hours ago": the Today view renders one *local* day, so seeding
    relative to the current instant puts every row on the previous day for anyone
    running the suite between midnight and the small hours. That is how five tests
    which had passed all week failed at 00:06 with nothing in the code changed.
    """
    zone = config.local_zone()
    return dt.datetime.combine(
        dt.datetime.now(zone).date(), dt.time(12, 0), tzinfo=zone
    ).astimezone(dt.UTC)


def _seed(session, *, muted: list[str] | None = None) -> int:
    """One mandate with a real event and three ticker items on the same day."""
    subject = Client(
        name="Zalando",
        aliases=[],
        keywords=[],
        alert_topics=[],
        muted_categories=muted or [],
    )
    session.add(subject)
    session.flush()
    now = _local_noon()
    rows = [
        ("BaFin rügt Zalando", Category.REGULATORIK, 8),
        ("Zalando-Aktie zeigt Stabilität", Category.FINANZEN, 4),
        ("Zalando-Aktie klettert deutlich", Category.FINANZEN, 4),
        ("Zalando-Aktie bleibt stabil", Category.FINANZEN, 5),
    ]
    for i, (title, category, importance) in enumerate(rows):
        article = Article(
            title=title,
            url=f"https://ex.de/{i}",
            source="Handelsblatt" if i == 0 else f"Boerse-{i}",
            published_at=now - dt.timedelta(hours=i + 1),
            fetched_at=now,
            summary_text=None,
            language="de",
            title_hash=f"h{i:04d}",
        )
        session.add(article)
        session.flush()
        session.add(
            Analysis(is_relevant=True, 
                article_id=article.id,
                client_id=subject.id,
                summary="s",
                category=category,
                relevance_score=6,
                importance_score=importance,
                is_alert=False,
            )
        )
    session.commit()
    return subject.id


def test_the_muted_category_is_gone_from_the_day(factory, client):
    with factory() as session:
        _seed(session, muted=["finanzen"])

    body = client.get("/today").text

    assert "BaFin rügt Zalando" in body
    assert "Zalando-Aktie zeigt Stabilität" not in body


def test_the_day_says_how_much_it_muted_and_offers_it_back(factory, client):
    """A silently shorter day is worse than a noisy one: the reader has no way to
    tell a quiet market from a filter they set weeks ago and forgot."""
    with factory() as session:
        _seed(session, muted=["finanzen"])

    body = " ".join(client.get("/today").text.split())

    assert "3 stummgeschaltet" in body
    assert "show_muted=1" in body


def test_showing_them_brings_them_back(factory, client):
    with factory() as session:
        _seed(session, muted=["finanzen"])

    body = client.get("/today?show_muted=1").text

    assert "Zalando-Aktie zeigt Stabilität" in body
    assert "wieder ausblenden" in body


def test_without_a_preference_nothing_changes(factory, client):
    with factory() as session:
        _seed(session)

    body = " ".join(client.get("/today").text.split())

    assert "Zalando-Aktie zeigt Stabilität" in body
    assert "stummgeschaltet" not in body


def test_muting_is_per_client_not_portfolio_wide(factory, client):
    """"finanzen" is three ticker items a day for a retailer and the entire
    mandate for a bank."""
    with factory() as session:
        _seed(session, muted=["finanzen"])
        bank = Client(name="Sparkasse", aliases=[], keywords=[], alert_topics=[])
        session.add(bank)
        session.flush()
        article = Article(
            title="Sparkasse meldet Quartalszahlen",
            url="https://ex.de/bank",
            source="Handelsblatt",
            published_at=_local_noon(),
            fetched_at=_local_noon(),
            summary_text=None,
            language="de",
            title_hash="bank0001",
        )
        session.add(article)
        session.flush()
        session.add(
            Analysis(is_relevant=True, 
                article_id=article.id,
                client_id=bank.id,
                summary="s",
                category=Category.FINANZEN,
                relevance_score=7,
                importance_score=6,
                is_alert=False,
            )
        )
        session.commit()

    body = client.get("/today").text

    assert "Sparkasse meldet Quartalszahlen" in body
    assert "Zalando-Aktie zeigt Stabilität" not in body


def test_the_archive_keeps_everything_that_was_muted(factory, client):
    """Hiding, never discarding: the number in a client report must not change
    because somebody tidied their daily view."""
    with factory() as session:
        client_id = _seed(session, muted=["finanzen"])

    body = client.get(f"/client/{client_id}").text

    assert "Zalando-Aktie zeigt Stabilität" in body


def test_the_preference_survives_the_edit_form(factory, client):
    with factory() as session:
        client_id = _seed(session)

    client.post(
        f"/settings/clients/{client_id}",
        data={
            "name": "Zalando",
            "aliases": "",
            "industry": "",
            "country": "DE",
            "keywords": "",
            "alert_topics": "",
            "muted_categories": ["finanzen", "sonstiges"],
        },
    )

    with factory() as session:
        assert session.get(Client, client_id).muted_categories == [
            "finanzen",
            "sonstiges",
        ]


def test_unchecking_everything_clears_the_preference(factory, client):
    """An unchecked box sends nothing, so "mute nothing" arrives as an absent
    field — it has to be written, not skipped, or the filter can never be undone
    from the form that set it."""
    with factory() as session:
        client_id = _seed(session, muted=["finanzen"])

    client.post(
        f"/settings/clients/{client_id}",
        data={
            "name": "Zalando",
            "aliases": "",
            "industry": "",
            "country": "DE",
            "keywords": "",
            "alert_topics": "",
        },
    )

    with factory() as session:
        assert session.get(Client, client_id).muted_categories == []


def test_an_invented_category_is_ignored(factory, client):
    with factory() as session:
        client_id = _seed(session)

    client.post(
        f"/settings/clients/{client_id}",
        data={
            "name": "Zalando",
            "aliases": "",
            "industry": "",
            "country": "DE",
            "keywords": "",
            "alert_topics": "",
            "muted_categories": ["finanzen", "erfunden"],
        },
    )

    with factory() as session:
        assert session.get(Client, client_id).muted_categories == ["finanzen"]
