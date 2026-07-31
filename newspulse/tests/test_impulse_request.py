"""Drafting an impulse on request (newspulse.job.draft_impulse).

The sweep drafts only from material that arrived that morning — right for a daily
rhythm, wrong for a person asking the question directly. A mandate whose field was
quiet today may still have plenty worth saying from the past fortnight, and before
this the answer to "give me something to send" was an empty column.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from newspulse import angles, job
from newspulse.db import make_engine
from newspulse.ingest import FeedItem
from newspulse.models import Angle, Article, Base, Client, TopicHit
from newspulse.schemas import AngleDraft

_NOW = dt.datetime(2026, 7, 31, 10, 0, tzinfo=dt.UTC)


@pytest.fixture
def session():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as sess:
        yield sess


def _client(session, **over) -> Client:
    client = Client(
        name=over.get("name", "IB-7 Beauty Tech GmbH"),
        aliases=[],
        keywords=over.get("keywords", []),
        alert_topics=over.get("alert_topics", ["KI in der Kosmetik"]),
        country="DE",
    )
    session.add(client)
    session.commit()
    return client


def _stored_market(session, client, title, *, days_ago=10) -> Article:
    """Market material from before today — the case the sweep cannot use."""
    article = Article(
        title=title,
        url=f"https://ex.de/{abs(hash(title)) % 100000}",
        source="Cosmetics Business",
        published_at=_NOW - dt.timedelta(days=days_ago),
        fetched_at=_NOW - dt.timedelta(days=days_ago),
        summary_text=None,
        language="de",
        title_hash=str(abs(hash(title)) % 10**8),
    )
    session.add(article)
    session.flush()
    session.add(
        TopicHit(article_id=article.id, client_id=client.id, found_at=_NOW - dt.timedelta(days=days_ago))
    )
    session.commit()
    return article


def _draft(**over):
    def _suggest(sess, cli, material, **_):
        numbered = angles.developments(material)
        draft = AngleDraft(
            worth_sending=over.get("worth_sending", True),
            subject="KI-Rezepturen: die Prüfbarkeit entscheidet",
            message="Zwei Absätze Text." if over.get("worth_sending", True) else "",
            context="c",
            thesis="t",
            evidence=[0] if numbered else [],
        )
        return (draft, numbered) if draft.worth_sending else None

    return _suggest


def _no_fetch(url, since, **_):
    """The radar returns nothing new — the quiet morning this feature is for."""
    return []


def test_a_quiet_morning_still_yields_an_impulse(session, monkeypatch):
    """The whole point: stored material from the past weeks is enough."""
    client = _client(session)
    _stored_market(session, client, "Kosmetikbranche diskutiert KI-Rezepturen")
    monkeypatch.setattr(angles, "suggest", _draft())

    assert job.draft_impulse(session, client, fetch=_no_fetch, now=lambda: _NOW) is True
    assert session.scalar(select(func.count()).select_from(Angle)) == 1


def test_the_radar_is_refreshed_before_drafting(session, monkeypatch):
    """A click should pick up what appeared since the last sweep, not only what
    was already stored."""
    client = _client(session)
    seen: list[str] = []

    def _fetch(url, since, *, source=None, **_):
        seen.append(source or "")
        return [
            FeedItem(
                title="Neue Studie zu KI-Wirksamkeit",
                link="https://ex.de/neu",
                source="Global Cosmetics News",
                published_at=_NOW - dt.timedelta(hours=2),
                summary="Ein Satz.",
                language="de",
            )
        ]

    monkeypatch.setattr(angles, "suggest", _draft())

    assert job.draft_impulse(session, client, fetch=_fetch, now=lambda: _NOW) is True
    assert any("Themen-Radar" in s for s in seen)
    # The fresh item is stored and linked, so the market view shows it too.
    assert session.scalar(select(func.count()).select_from(TopicHit)) == 1


def test_a_client_without_themes_yields_nothing_and_costs_nothing(session, monkeypatch):
    """No themes, no radar, no question to ask — and no model call to waste."""
    client = _client(session, keywords=[], alert_topics=[])
    monkeypatch.setattr(
        angles, "suggest", lambda *a, **k: pytest.fail("must not ask the model")
    )

    assert job.draft_impulse(session, client, fetch=_no_fetch, now=lambda: _NOW) is False


def test_no_market_material_is_an_honest_no(session, monkeypatch):
    monkeypatch.setattr(
        angles, "suggest", lambda *a, **k: pytest.fail("must not ask the model")
    )
    client = _client(session)

    assert job.draft_impulse(session, client, fetch=_no_fetch, now=lambda: _NOW) is False


def test_the_model_declining_stores_nothing(session, monkeypatch):
    """Asking on demand does not force an opening into existence: manufactured
    urgency is what the whole design refuses."""
    client = _client(session)
    _stored_market(session, client, "Irgendeine Marktmeldung")
    monkeypatch.setattr(angles, "suggest", _draft(worth_sending=False))

    assert job.draft_impulse(session, client, fetch=_no_fetch, now=lambda: _NOW) is False
    assert session.scalar(select(func.count()).select_from(Angle)) == 0


def test_material_older_than_the_window_is_not_used(session, monkeypatch):
    client = _client(session)
    _stored_market(session, client, "Uralte Meldung", days_ago=200)
    monkeypatch.setattr(
        angles, "suggest", lambda *a, **k: pytest.fail("must not ask the model")
    )

    assert job.draft_impulse(session, client, fetch=_no_fetch, now=lambda: _NOW) is False
