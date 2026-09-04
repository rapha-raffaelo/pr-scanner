"""The source list, which is now the app's primary navigation.

It is the one component every page renders, and it is fed by a template global
rather than by the routes, so nothing else in the suite would notice if it
silently emptied. These tests are the notice.
"""

from __future__ import annotations

import datetime as dt
import re

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse.models import Analysis, Article, Base, Category, Client
from newspulse.web.app import create_app, get_db


def _local_noon(day: dt.date) -> dt.datetime:
    """Noon on ``day`` in the machine's local tz, so a run at 00:06 does not seed
    "today" into yesterday and fail on a technicality."""
    return dt.datetime.combine(day, dt.time(12, 0), tzinfo=dt.datetime.now().astimezone().tzinfo)


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
    from fastapi.testclient import TestClient

    return TestClient(app)


def _sidebar(body: str) -> str:
    return body.split('<aside class="side">', 1)[1].split("</aside>", 1)[0]


def _names(body: str) -> list[str]:
    return re.findall(r'<span class="side__nm">([^<]+)</span>', _sidebar(body))


def _badges(body: str) -> list[str]:
    return re.findall(r'<span class="side__badge">(\d+)</span>', _sidebar(body))


def _alert(session, client_obj, *, day, url):
    article = Article(
        title="Rückruf", url=url, source="Handelsblatt",
        published_at=_local_noon(day), fetched_at=_local_noon(day),
        summary_text="Feed-Snippet.", language="de", title_hash=url[-16:],
    )
    session.add(article)
    session.flush()
    session.add(Analysis(is_relevant=True, 
        article_id=article.id, client_id=client_obj.id, summary="Ein Satz.",
        category=Category.KRISE, relevance_score=5, importance_score=8, is_alert=True,
    ))


def test_every_page_lists_the_mandates(factory, client):
    """The roster is a template global, so it has to appear on pages whose route
    knows nothing about it — that is the whole reason it is a global."""
    with factory() as s:
        s.add_all([
            Client(name="Alpha AG", aliases=[], keywords=[], alert_topics=[]),
            Client(name="Beta AG", aliases=[], keywords=[], alert_topics=[]),
        ])
        s.commit()

    for path in ("/", "/today", "/archive", "/contacts", "/settings"):
        assert _names(client.get(path).text) == ["Alpha AG", "Beta AG"], path


def test_a_competitor_is_not_a_mandate(factory, client):
    """Same rule as the portfolio: a benchmark is what a mandate is measured
    against, and listing it as a peer invites reading its coverage as work."""
    with factory() as s:
        s.add_all([
            Client(name="Alpha AG", aliases=[], keywords=[], alert_topics=[]),
            Client(name="About You", aliases=[], keywords=[], alert_topics=[],
                   is_competitor=True),
        ])
        s.commit()

    assert _names(client.get("/").text) == ["Alpha AG"]


def test_the_badge_counts_todays_alerts_only(factory, client):
    """The badge is the only red thing in the navigation, so it must mean one
    thing: an alert published today. Yesterday's is history and belongs to the
    archive, not to a count that says "look here now"."""
    today = dt.datetime.now().astimezone().date()
    with factory() as s:
        alpha = Client(name="Alpha AG", aliases=[], keywords=[], alert_topics=[])
        beta = Client(name="Beta AG", aliases=[], keywords=[], alert_topics=[])
        s.add_all([alpha, beta])
        s.flush()
        _alert(s, alpha, day=today, url="https://ex.de/a1")
        _alert(s, alpha, day=today, url="https://ex.de/a2")
        _alert(s, beta, day=today - dt.timedelta(days=1), url="https://ex.de/b1")
        s.commit()

    body = client.get("/").text
    # Alpha carries a 2; Beta carries nothing rather than a grey 0, because a
    # badge that is always present stops being a signal.
    assert _badges(body) == ["2"]
    assert 'class="side__badge"' in _sidebar(body)


def test_the_current_mandate_is_marked(factory, client):
    """Selection follows the whole workspace, not one tab of it: the sidebar has
    to keep saying whose desk this is while the tabs move underneath."""
    with factory() as s:
        alpha = Client(name="Alpha AG", aliases=[], keywords=[], alert_topics=[])
        s.add(alpha)
        s.commit()
        alpha_id = alpha.id

    for path in (f"/client/{alpha_id}", f"/client/{alpha_id}/advice",
                 f"/client/{alpha_id}/profil"):
        side = _sidebar(client.get(path).text)
        row = [ln for ln in side.splitlines() if f'href="/client/{alpha_id}/heute"' in ln]
        assert row and "is-on" in row[0], path


def test_an_empty_portfolio_renders_no_mandate_section(factory, client):
    """A heading over nothing reads as a page that failed to load."""
    side = _sidebar(client.get("/").text)
    assert "side__cl" not in side
    assert "Mandanten" not in side
