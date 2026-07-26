"""Route tests for the Today view (NP-07, DEC-1 option C: two-pane triage).

These drive the single ``GET /`` route through FastAPI's TestClient against a
seeded in-memory SQLite database — interface-level, not the whole app stack. The
``get_db`` dependency is overridden to hand the route a session bound to the
fixture engine, so no real database file or daily job is involved.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse.models import Analysis, Article, Base, Category, Client, Run, RunStatus
from newspulse.web.app import create_app, get_db, safe_url
from newspulse.web.routes import today

# A fixed reference day so tests never depend on the wall clock. Coverage is
# seeded at local noon on this day and the page requested via ?date=.
_TEST_DAY = dt.date(2026, 7, 20)


def _local_noon(day: dt.date) -> dt.datetime:
    """Noon on ``day`` in the machine's local tz — safely inside the local day
    window the route computes, regardless of the runner's timezone."""
    local_tz = dt.datetime.now().astimezone().tzinfo
    return dt.datetime.combine(day, dt.time(12, 0), tzinfo=local_tz)


@pytest.fixture
def factory():
    """A sessionmaker bound to a fresh in-memory database with the schema built.

    StaticPool keeps every session on the same single connection so the seeded
    schema and rows are visible to the route's session — a plain ``:memory:``
    engine gives each new connection its own empty database.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def client(factory):
    """A TestClient whose route session is bound to the fixture database."""
    app = create_app()

    def _override_get_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


def _seed_client(session, name: str) -> Client:
    obj = Client(name=name)
    session.add(obj)
    session.flush()
    return obj


def _seed_coverage(
    session,
    *,
    client_name: str,
    title: str,
    url: str,
    importance: int,
    is_alert: bool,
    published_at: dt.datetime,
    source: str = "Handelsblatt",
    summary: str = "Ein Satz Zusammenfassung.",
    category: Category = Category.PRODUKT,
) -> None:
    client = _seed_client(session, client_name)
    article = Article(
        title=title,
        url=url,
        source=source,
        published_at=published_at,
        fetched_at=published_at,
        summary_text="Feed-Snippet.",
        language="de",
        title_hash=url[-8:],
    )
    session.add(article)
    session.flush()
    session.add(
        Analysis(
            article_id=article.id,
            client_id=client.id,
            summary=summary,
            category=category,
            relevance_score=importance,
            importance_score=importance,
            is_alert=is_alert,
        )
    )


def test_today_renders_items_in_importance_order_with_alerts_surfaced(factory, client):
    """The feed lists items by importance desc with alerts surfaced above the rest."""
    with factory() as s:
        _seed_coverage(
            s, client_name="Alpha AG", title="ALERTHEADLINE Rueckruf",
            url="https://ex.de/alert-a", importance=9, is_alert=True,
            published_at=_local_noon(_TEST_DAY),
        )
        _seed_coverage(
            s, client_name="Beta AG", title="LOWHEADLINE Randnotiz",
            url="https://ex.de/low-b", importance=5, is_alert=False,
            published_at=_local_noon(_TEST_DAY),
        )
        _seed_coverage(
            s, client_name="Gamma AG", title="MIDHEADLINE Wichtig",
            url="https://ex.de/mid-c", importance=8, is_alert=False,
            published_at=_local_noon(_TEST_DAY),
        )
        s.commit()

    body = client.get("/", params={"date": _TEST_DAY.isoformat()}).text

    # Within the ranked feed pane: alert(9) before mid(8) before low(5).
    feed = body.split("triage__feed", 1)[1]
    pos_alert = feed.index("ALERTHEADLINE")
    pos_mid = feed.index("MIDHEADLINE")
    pos_low = feed.index("LOWHEADLINE")
    assert pos_alert < pos_mid < pos_low

    # The alert is surfaced in the left rail (an alert card), the others are not.
    rail = body.split("triage__feed", 1)[0]
    assert "alert-card" in rail
    assert "ALERTHEADLINE" in rail
    assert "MIDHEADLINE" not in rail


def test_today_item_shows_all_required_fields(factory, client):
    """Each row carries headline out-link, source, time, summary, category,
    importance and client (the DEC-1 field set)."""
    published = _local_noon(_TEST_DAY)
    with factory() as s:
        _seed_coverage(
            s, client_name="Delta AG", title="Delta launcht Produkt",
            url="https://ex.de/delta", importance=6, is_alert=False,
            published_at=published, source="FAZ",
            summary="Delta stellt ein neues Produkt vor.", category=Category.PRODUKT,
        )
        s.commit()

    body = client.get("/", params={"date": _TEST_DAY.isoformat()}).text

    assert 'href="https://ex.de/delta"' in body
    assert 'target="_blank"' in body  # headline links out
    assert "FAZ" in body  # source
    assert published.strftime("%H:%M") in body  # time
    assert "Delta stellt ein neues Produkt vor." in body  # summary
    assert "produkt" in body  # category tag
    assert "6/10" in body  # importance
    assert "Delta AG" in body  # client


def test_empty_day_renders_clean_empty_state(client):
    """A day with no coverage renders an empty state, not an error."""
    resp = client.get("/", params={"date": _TEST_DAY.isoformat()})
    assert resp.status_code == 200
    assert "Keine Berichterstattung" in resp.text


def test_header_shows_last_run_status(factory, client):
    """The header reflects the latest runs row: time, status, articles checked."""
    with factory() as s:
        s.add(
            Run(
                started_at=dt.datetime(2026, 7, 20, 6, 0, tzinfo=dt.UTC),
                finished_at=dt.datetime(2026, 7, 20, 6, 5, tzinfo=dt.UTC),
                status=RunStatus.OK,
                articles_found=137,
                errors=[],
            )
        )
        s.commit()

    body = client.get("/").text
    assert "137 Artikel geprüft" in body
    assert "Feeds ok" in body
    assert "ok" in body


def test_header_without_any_run_shows_placeholder(client):
    """With no runs yet the header shows a placeholder rather than crashing."""
    body = client.get("/").text
    assert "Noch kein Lauf" in body


def test_coverage_from_another_day_is_excluded(factory, client):
    """Only the requested local day's coverage appears; a prior day is filtered out."""
    with factory() as s:
        _seed_coverage(
            s, client_name="Alpha AG", title="HEUTE Story",
            url="https://ex.de/today", importance=7, is_alert=False,
            published_at=_local_noon(_TEST_DAY),
        )
        _seed_coverage(
            s, client_name="Beta AG", title="GESTERN Story",
            url="https://ex.de/yesterday", importance=9, is_alert=False,
            published_at=_local_noon(_TEST_DAY - dt.timedelta(days=1)),
        )
        s.commit()

    body = client.get("/", params={"date": _TEST_DAY.isoformat()}).text
    assert "HEUTE Story" in body
    assert "GESTERN Story" not in body


def test_javascript_url_is_not_rendered_into_href(factory, client):
    """A javascript: feed URL is blanked, never emitted as a clickable href.

    Jinja autoescape does not block dangerous URL schemes, so an attacker-
    influenced feed URL (``javascript:...``) would otherwise become a script link
    running in the app's origin. The safe_url filter must strip it.
    """
    with factory() as s:
        _seed_coverage(
            s, client_name="Eta AG", title="XSSHEADLINE",
            url="javascript:alert(document.domain)//", importance=8, is_alert=True,
            published_at=_local_noon(_TEST_DAY),
        )
        s.commit()

    body = client.get("/", params={"date": _TEST_DAY.isoformat()}).text
    assert "XSSHEADLINE" in body  # the item still renders
    assert "javascript:alert" not in body  # the dangerous scheme is gone
    assert 'href=""' in body  # blanked to an empty href


def test_safe_url_blanks_dangerous_schemes_but_keeps_http():
    """The scheme allow-list passes http(s)/relative and blanks the rest."""
    assert safe_url("https://ex.de/a") == "https://ex.de/a"
    assert safe_url("/relative/path") == "/relative/path"
    assert safe_url("javascript:alert(1)") == ""
    assert safe_url("data:text/html,<script>1</script>") == ""
    assert safe_url("java\tscript:alert(1)") == ""  # tab-strip bypass closed


def test_zero_relevance_analysis_is_excluded(factory, client):
    """A relevance_score=0 analysis (non-matching client pair) is not surfaced."""
    with factory() as s:
        _seed_coverage(
            s, client_name="Zeta AG", title="NOISE Irrelevant",
            url="https://ex.de/noise", importance=0, is_alert=False,
            published_at=_local_noon(_TEST_DAY),
        )
        s.commit()

    body = client.get("/", params={"date": _TEST_DAY.isoformat()}).text
    assert "NOISE Irrelevant" not in body
    assert "Keine Berichterstattung" in body  # falls through to the empty state


def test_local_zone_resolves_dst_aware_not_frozen_offset(monkeypatch):
    """The resolved local zone honors DST, unlike a single frozen offset.

    The old ``datetime.now().astimezone().tzinfo`` returned a fixed offset, so a
    day in the other DST regime was bounded with the wrong offset (±1h). A
    DST-aware zone yields different offsets for winter vs summer days.
    """
    monkeypatch.setenv("TZ", "Europe/Berlin")
    zone = today._resolve_local_zone()
    winter = dt.datetime(2026, 1, 15, tzinfo=zone).utcoffset()
    summer = dt.datetime(2026, 7, 15, tzinfo=zone).utcoffset()
    assert winter == dt.timedelta(hours=1)
    assert summer == dt.timedelta(hours=2)
    assert winter != summer  # a frozen offset would make these equal
