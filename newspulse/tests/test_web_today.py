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
    feed = body.split('class="feedcol"', 1)[1]
    pos_alert = feed.index("ALERTHEADLINE")
    pos_mid = feed.index("MIDHEADLINE")
    pos_low = feed.index("LOWHEADLINE")
    assert pos_alert < pos_mid < pos_low

    # The alert is surfaced in the left rail (an alert card), the others are not.
    rail = body.split('class="feedcol"', 1)[0]
    assert "acard" in rail
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
    # Importance rides in the score badge; its title carries the full scale.
    assert 'title="Wichtigkeit 6 von 10"' in body
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


# --- Category filter -----------------------------------------------------------


def _seed_day_mix(factory):
    """Three categories on one day, so filtering has something to narrow."""
    with factory() as s:
        _seed_coverage(
            s, client_name="Alpha AG", title="KRISEHEADLINE Werk schliesst",
            url="https://ex.de/k1", importance=9, is_alert=True,
            published_at=_local_noon(_TEST_DAY), category=Category.KRISE,
        )
        _seed_coverage(
            s, client_name="Beta AG", title="FINANZHEADLINE Aktie faellt",
            url="https://ex.de/f1", importance=5, is_alert=False,
            published_at=_local_noon(_TEST_DAY), category=Category.FINANZEN,
        )
        _seed_coverage(
            s, client_name="Gamma AG", title="PRODUKTHEADLINE Neue App",
            url="https://ex.de/p1", importance=4, is_alert=False,
            published_at=_local_noon(_TEST_DAY), category=Category.PRODUKT,
        )
        s.commit()


def test_category_filter_narrows_the_day_to_one_category(factory, client):
    _seed_day_mix(factory)
    body = client.get("/", params={"date": _TEST_DAY.isoformat(), "category": "finanzen"}).text
    assert "FINANZHEADLINE" in body
    assert "KRISEHEADLINE" not in body
    assert "PRODUKTHEADLINE" not in body


def test_category_filter_also_narrows_the_alert_rail(factory, client):
    """The rail is the filtered day's alerts, not the whole day's — otherwise a
    filtered view would show alerts for stories no longer in the feed."""
    _seed_day_mix(factory)
    body = client.get("/", params={"date": _TEST_DAY.isoformat(), "category": "finanzen"}).text
    rail = body.split('class="feedcol"', 1)[0]
    assert "KRISEHEADLINE" not in rail


def test_category_dropdown_offers_only_categories_present_that_day(factory, client):
    """An option that would return an empty page is worse than no option."""
    _seed_day_mix(factory)
    body = client.get("/", params={"date": _TEST_DAY.isoformat()}).text
    for present in ("krise", "finanzen", "produkt"):
        assert f'value="{present}"' in body
    for absent in ("personalie", "wettbewerb", "regulatorik"):
        assert f'value="{absent}"' not in body


def test_unknown_category_degrades_to_no_filter(factory, client):
    """A stale or hand-typed category shows the whole day, not an empty page."""
    _seed_day_mix(factory)
    body = client.get("/", params={"date": _TEST_DAY.isoformat(), "category": "nonsense"}).text
    assert "KRISEHEADLINE" in body
    assert "FINANZHEADLINE" in body
    assert "PRODUKTHEADLINE" in body


def test_filter_reports_how_many_items_it_hid(factory, client):
    _seed_day_mix(factory)
    body = client.get("/", params={"date": _TEST_DAY.isoformat(), "category": "finanzen"}).text
    assert "2 ausgeblendet" in body


def test_outlet_tier_breaks_ties_without_changing_the_score(factory, client):
    """Among equally-scored stories the better outlet ranks first — but the
    displayed score stays the model's own and nothing is dropped."""
    with factory() as s:
        _seed_coverage(
            s, client_name="Alpha AG", title="WIREHEADLINE Aktie stabil",
            url="https://ex.de/w1", importance=7, is_alert=False,
            published_at=_local_noon(_TEST_DAY), source="Ad-hoc-news.de",
        )
        _seed_coverage(
            s, client_name="Beta AG", title="QUALITYHEADLINE Werk schliesst",
            url="https://ex.de/q1", importance=7, is_alert=False,
            published_at=_local_noon(_TEST_DAY), source="FAZ",
        )
        s.commit()

    body = client.get("/", params={"date": _TEST_DAY.isoformat()}).text
    feed = body.split('class="feedcol"', 1)[1]
    assert feed.index("QUALITYHEADLINE") < feed.index("WIREHEADLINE")
    # Both are still present: tier reorders, it never hides.
    assert "WIREHEADLINE" in feed
    # And the wire item still shows the model's own 7, not a weighted 5.
    assert 'title="Wichtigkeit 7 von 10"' in feed


def test_a_higher_score_still_outranks_a_better_outlet(factory, client):
    """Tier is a tiebreaker, not a promotion: it must never reorder across scores."""
    with factory() as s:
        _seed_coverage(
            s, client_name="Alpha AG", title="WIREHEADLINE Wichtige Meldung",
            url="https://ex.de/w2", importance=9, is_alert=False,
            published_at=_local_noon(_TEST_DAY), source="Ad-hoc-news.de",
        )
        _seed_coverage(
            s, client_name="Beta AG", title="QUALITYHEADLINE Randnotiz",
            url="https://ex.de/q2", importance=6, is_alert=False,
            published_at=_local_noon(_TEST_DAY), source="FAZ",
        )
        s.commit()

    feed = client.get("/", params={"date": _TEST_DAY.isoformat()}).text.split('class="feedcol"', 1)[1]
    assert feed.index("WIREHEADLINE") < feed.index("QUALITYHEADLINE")


def test_syndicated_coverage_occupies_one_slot_with_its_pickup_count(factory, client):
    """Three outlets running one dpa story is one line carrying "3× aufgegriffen",
    not three lines crowding the rail."""
    with factory() as s:
        for outlet in ("SZ.de", "Tagesspiegel", "Baden Online"):
            _seed_coverage(
                s, client_name="Alpha AG",
                title=f"Bafin ruegt Zalando wegen fehlender Angaben zur Uebernahme - {outlet}",
                url=f"https://ex.de/{outlet}", importance=8, is_alert=True,
                published_at=_local_noon(_TEST_DAY), source=outlet,
            )
        s.commit()

    body = client.get("/", params={"date": _TEST_DAY.isoformat()}).text
    assert "3× aufgegriffen" in body
    # One card in the rail, not three.
    rail = body.split('class="feedcol"', 1)[0]
    assert rail.count('class="acard"') == 1
    # And one row in the feed.
    feed = body.split('class="feedcol"', 1)[1]
    assert feed.count('class="item') == 1
    # The other outlets are still reachable, not discarded.
    assert "Tagesspiegel" in body and "Baden Online" in body


def test_distinct_stories_are_not_collapsed_in_the_view(factory, client):
    with factory() as s:
        _seed_coverage(
            s, client_name="Alpha AG", title="ERSTE Zalando schliesst Standort Erfurt komplett",
            url="https://ex.de/s1", importance=9, is_alert=False,
            published_at=_local_noon(_TEST_DAY), source="MDR.de",
        )
        _seed_coverage(
            s, client_name="Alpha AG", title="ZWEITE Zalando startet Same-Day-Lieferung in Staedten",
            url="https://ex.de/s2", importance=5, is_alert=False,
            published_at=_local_noon(_TEST_DAY), source="Spiegel",
        )
        s.commit()

    feed = client.get("/", params={"date": _TEST_DAY.isoformat()}).text.split('class="feedcol"', 1)[1]
    assert feed.count('class="item') == 2
    assert "aufgegriffen" not in feed


def test_competitor_coverage_stays_out_of_the_daily_triage(factory, client):
    """A rival's coverage is monitored for share of voice, not for triage —
    mixing it in makes the day look busier than the work actually is."""
    with factory() as s:
        mandate = _seed_client(s, "Alpha AG")
        rival = Client(name="Beta AG", is_competitor=True)
        s.add(rival)
        s.flush()
        for owner, title in ((mandate, "MANDANT Story"), (rival, "RIVALE Story")):
            art = Article(
                title=title, url=f"https://ex.de/{title}", source="FAZ",
                published_at=_local_noon(_TEST_DAY), fetched_at=_local_noon(_TEST_DAY),
                summary_text="s", language="de", title_hash=title[:8],
            )
            s.add(art)
            s.flush()
            s.add(Analysis(
                article_id=art.id, client_id=owner.id, summary="Zusammenfassung.",
                category=Category.PRODUKT, relevance_score=5, importance_score=7,
                is_alert=False,
            ))
        s.commit()

    body = client.get("/", params={"date": _TEST_DAY.isoformat()}).text
    assert "MANDANT Story" in body
    assert "RIVALE Story" not in body


def test_the_importance_number_is_labelled(factory, client):
    """A bare digit in a badge explains nothing on first sight."""
    with factory() as s:
        _seed_coverage(
            s, client_name="Alpha AG", title="Irgendeine Meldung heute",
            url="https://ex.de/x", importance=7, is_alert=False,
            published_at=_local_noon(_TEST_DAY),
        )
        s.commit()
    body = client.get("/", params={"date": _TEST_DAY.isoformat()}).text
    assert "Wichtigkeit 0–10" in body


def test_every_page_offers_a_refresh(client):
    """Fetching news is wanted from anywhere, not three clicks deep in settings."""
    for path in ("/", "/clients", "/archive", "/settings"):
        body = client.get(path).text
        assert 'action="/settings/run"' in body, path
        assert "Aktualisieren" in body, path
