"""Route tests for the Today view (NP-07, DEC-1 option C: two-pane triage).

These drive the single ``GET /`` route through FastAPI's TestClient against a
seeded in-memory SQLite database — interface-level, not the whole app stack. The
``get_db`` dependency is overridden to hand the route a session bound to the
fixture engine, so no real database file or daily job is involved.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import config
from newspulse.models import Analysis, Article, Base, Category, Client, Run, RunStatus
from newspulse.web.app import create_app, get_db, safe_url
from newspulse.web.routes import today

# A fixed reference day so tests never depend on the wall clock. Coverage is
# seeded at local noon on this day and the page requested via ?date=.
_TEST_DAY = dt.date(2026, 7, 20)


def _local_noon(day: dt.date) -> dt.datetime:
    """Noon on ``day`` in the *display* zone — safely inside the day window the
    route computes, regardless of where the test runner's own clock is set."""
    return dt.datetime.combine(day, dt.time(12, 0), tzinfo=config.local_zone())


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


def test_today_uses_the_configured_display_zone(monkeypatch):
    """The day window comes from the configured zone, not the host's clock.

    The zone itself is resolved in :mod:`newspulse.config` (tested there); this
    pins that the route reads it from there, so a UTC container can no longer
    decide when the reader's day starts.
    """
    monkeypatch.setattr(config, "LOCAL_ZONE", ZoneInfo("Europe/Berlin"))
    assert today._local_tz() is config.local_zone()
    start, end = today._day_bounds_utc(dt.date(2026, 7, 20))
    # Berlin is UTC+2 in July, so the local day starts at 22:00 UTC the night
    # before — the whole point of not bounding the day in the server's zone.
    assert start == dt.datetime(2026, 7, 19, 22, 0, tzinfo=dt.UTC)
    assert end == dt.datetime(2026, 7, 20, 22, 0, tzinfo=dt.UTC)


def test_header_run_time_is_shown_in_the_reader_zone(factory, client, monkeypatch):
    """Regression: the header showed the run time in the container's zone (UTC).

    A sweep at 10:00 Berlin was rendered "Letzter Lauf 08:00 Uhr" on Railway,
    because the timestamp is stored UTC and nothing converted it for display.
    """
    monkeypatch.setattr(config, "LOCAL_ZONE", ZoneInfo("Europe/Berlin"))
    with factory() as s:
        s.add(
            Run(
                started_at=dt.datetime(2026, 7, 30, 7, 55, tzinfo=dt.UTC),
                finished_at=dt.datetime(2026, 7, 30, 8, 0, tzinfo=dt.UTC),
                status=RunStatus.OK,
                articles_found=1,
                errors=[],
            )
        )
        s.commit()

    body = client.get("/", params={"date": _TEST_DAY.isoformat()}).text

    assert "Letzter Lauf 10:00 Uhr" in body
    assert "08:00 Uhr" not in body


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


def test_the_importance_score_is_not_shown_but_still_orders_the_feed(factory, client):
    """The raw 0-10 read as noise on a triage screen, so it was removed from the
    row. It must still do its job: rank the day and drive the alert threshold."""
    with factory() as s:
        _seed_coverage(
            s, client_name="Alpha AG", title="NIEDRIG Randnotiz heute",
            url="https://ex.de/lo", importance=3, is_alert=False,
            published_at=_local_noon(_TEST_DAY),
        )
        _seed_coverage(
            s, client_name="Beta AG", title="HOCH Wichtige Meldung heute",
            url="https://ex.de/hi", importance=9, is_alert=False,
            published_at=_local_noon(_TEST_DAY),
        )
        s.commit()
    body = client.get("/", params={"date": _TEST_DAY.isoformat()}).text
    assert "Wichtigkeit" not in body
    feed = body.split('class="feedcol"', 1)[1]
    assert feed.index("HOCH") < feed.index("NIEDRIG")


def test_every_page_offers_a_refresh(client):
    """Fetching news is wanted from anywhere, not three clicks deep in settings."""
    for path in ("/", "/clients", "/archive", "/settings"):
        body = client.get(path).text
        assert 'action="/settings/run"' in body, path
        assert "Aktualisieren" in body, path


def test_a_stored_logo_survives_rendering_but_an_svg_never_does(client):
    """safe_url blanks every non-http scheme, which is right for a feed link and
    wrong for a logo we fetched and validated ourselves. logo_src is the narrow
    exception — and it must not admit SVG, which is executable markup."""
    from newspulse.web.app import logo_src

    png = "data:image/png;base64,iVBORw0KGgo="
    assert logo_src(png) == png
    assert logo_src("https://example.com/logo.png") == "https://example.com/logo.png"
    assert logo_src("data:image/svg+xml;base64,PHN2Zz4=") == ""
    assert logo_src("javascript:alert(1)") == ""
    assert logo_src("http://example.com/logo.png") == ""  # https only
    assert logo_src(None) == ""


def test_the_day_can_be_filtered_to_one_mandate(factory, client):
    with factory() as s:
        _seed_coverage(
            s, client_name="Alpha AG", title="ALPHA Meldung von heute",
            url="https://ex.de/a", importance=7, is_alert=False,
            published_at=_local_noon(_TEST_DAY),
        )
        _seed_coverage(
            s, client_name="Beta AG", title="BETA Meldung von heute",
            url="https://ex.de/b", importance=7, is_alert=False,
            published_at=_local_noon(_TEST_DAY),
        )
        s.commit()
        alpha_id = s.query(Client).filter_by(name="Alpha AG").one().id

    both = client.get("/", params={"date": _TEST_DAY.isoformat()}).text
    assert "ALPHA" in both and "BETA" in both

    only = client.get("/", params={"date": _TEST_DAY.isoformat(), "client": alpha_id}).text
    assert "ALPHA" in only
    assert "BETA Meldung" not in only


def test_an_unknown_client_id_shows_the_whole_day(factory, client):
    """Same posture as the category filter: never an unexplainable empty page."""
    with factory() as s:
        _seed_coverage(
            s, client_name="Alpha AG", title="ALPHA Meldung von heute",
            url="https://ex.de/a", importance=7, is_alert=False,
            published_at=_local_noon(_TEST_DAY),
        )
        s.commit()
    body = client.get("/", params={"date": _TEST_DAY.isoformat(), "client": 9999}).text
    assert "ALPHA" in body


def test_a_competitor_is_not_offered_as_a_filter(factory, client):
    """The strip is a way into your own day, not a client manager."""
    with factory() as s:
        _seed_client(s, "Alpha AG")
        s.add(Client(name="Rivale AG", is_competitor=True))
        s.commit()
    body = client.get("/", params={"date": _TEST_DAY.isoformat()}).text
    assert "Rivale AG" not in body


# --- The empty day ------------------------------------------------------------
#
# Regression from live use: after a successful first sweep the operator opened
# Heute, saw "Keine Berichterstattung für <today>", and concluded the tool was
# broken — while 40 analysed articles sat in the archive. German feeds yield a
# handful of relevant items per mandate per day, so an empty or near-empty day is
# the normal case, not the failure case. The empty state has to say where the
# coverage is, or it reads as a fault every quiet morning.


def test_empty_day_points_at_recent_coverage_in_the_archive(factory, client):
    today = dt.datetime.now().astimezone().date()
    with factory() as session:
        for offset in (2, 3, 4):
            _seed_coverage(
                session,
                client_name="Zalando",
                title=f"Zalando meldet Zahlen {offset}",
                url=f"https://example.de/z{offset}",
                importance=6,
                is_alert=False,
                published_at=dt.datetime.combine(
                    today - dt.timedelta(days=offset), dt.time(9, 0), tzinfo=dt.UTC
                ),
            )
        session.commit()

    body = client.get("/").text

    assert "Keine Berichterstattung" in body, "today genuinely has nothing"
    hint = body.split('empty-state-hint')[1].split('</p>')[0]
    assert "3" in hint, f"the hint must carry the count of recent articles: {hint!r}"
    assert 'href="/archive"' in hint, "and a way to reach them"


def test_a_truly_empty_database_offers_setup_not_the_archive(factory, client):
    """Nothing anywhere is a different problem, and needs different advice.

    Pointing at an empty archive would send the operator somewhere that confirms
    nothing; the useful next step is adding mandates and running a backfill.
    """
    body = client.get("/").text

    assert "Keine Berichterstattung" in body
    assert 'href="/settings"' in body
    assert 'href="/archive"' not in body.split("empty-state")[-1]


def test_a_day_with_coverage_shows_no_hint(factory, client):
    """The hint belongs to the empty state only — it must not clutter a busy day."""
    today = dt.datetime.now().astimezone().date()
    with factory() as session:
        _seed_coverage(
            session,
            client_name="Zalando",
            title="Zalando heute",
            url="https://example.de/heute",
            importance=8,
            is_alert=False,
            published_at=dt.datetime.combine(today, dt.time(9, 0), tzinfo=dt.UTC),
        )
        session.commit()

    body = client.get("/").text

    assert "empty-state-hint" not in body


# --- The third column: positioning drafts --------------------------------------
#
# What to *send* a mandate, drafted by the daily run off market coverage the mandate
# is not in (newspulse.angles). It is the one column that is not a list of articles,
# so what it must get right is different: the sendable text has to be separable from
# the reasoning around it, and it must not show a draft from another day.


def _seed_angle(session, *, client_name="Arrakis", generated_at, **over):
    from newspulse.models import Angle

    client = session.scalar(select(Client).where(Client.name == client_name))
    if client is None:
        client = _seed_client(session, client_name)
    angle = Angle(
        client_id=client.id,
        generated_at=generated_at,
        subject=over.get("subject", "Börsenschließungen: Liquidität als Infrastruktur"),
        message=over.get(
            "message",
            "Die aktuellen Schließungen zeigen nicht das Ende des Marktes.\n\n"
            "Projekte müssen Liquidität als eigene Infrastruktur betrachten.",
        ),
        context=over.get("context", "Mehrere Handelsplätze kündigen die Abwicklung an."),
        credibility=over.get("credibility", "Berührt den Kern des Mandanten."),
        thesis=over.get("thesis", "Der Markt konsolidiert."),
        overclaim=over.get("overclaim", "Zentrale Börsen verschwinden."),
        statements=over.get("statements", ["Liquidität ist Infrastruktur."]),
        article_ids=over.get("article_ids", []),
    )
    session.add(angle)
    session.commit()
    return angle


def _noon_utc(day: dt.date) -> dt.datetime:
    """Midday in the display zone, i.e. safely inside that local day's window."""
    return dt.datetime.combine(day, dt.time(12, 0), tzinfo=config.local_zone())


def test_the_impulse_column_renders_the_draft_and_its_reasoning(factory, client):
    with factory() as session:
        _seed_angle(session, generated_at=_noon_utc(_TEST_DAY))

    body = client.get("/", params={"date": _TEST_DAY.isoformat()}).text

    column = body.split('class="anglecol"', 1)[1]
    assert "Börsenschließungen: Liquidität als Infrastruktur" in column
    assert "Liquidität als eigene Infrastruktur betrachten" in column
    assert "Der Markt konsolidiert." in column
    # The rejected reading is shown, not hidden: it is how the reader checks that
    # the message did not drift into it.
    assert "Zentrale Börsen verschwinden." in column
    assert "Arrakis" in column


def test_only_the_sendable_text_sits_in_the_copy_target(factory, client):
    """The copy button takes one element. Our reasoning must not be inside it, or
    it lands in a client's inbox."""
    with factory() as session:
        angle = _seed_angle(session, generated_at=_noon_utc(_TEST_DAY))

    body = client.get("/", params={"date": _TEST_DAY.isoformat()}).text

    target = body.split(f'id="impulse-text-{angle.id}"', 1)[1].split("</div>", 1)[0]
    assert "Liquidität als eigene Infrastruktur" in target
    assert "Der Markt konsolidiert." not in target
    assert "Berührt den Kern" not in target
    assert f'data-copy-from="impulse-text-{angle.id}"' in body


def test_a_draft_from_another_day_is_not_shown(factory, client):
    """The page is a day. Yesterday's draft is not today's work."""
    with factory() as session:
        _seed_angle(
            session,
            generated_at=_noon_utc(_TEST_DAY - dt.timedelta(days=1)),
            subject="GESTERN",
        )

    body = client.get("/", params={"date": _TEST_DAY.isoformat()}).text

    assert "GESTERN" not in body
    assert "Kein Anlass heute." in body


def test_the_column_follows_the_client_filter(factory, client):
    """Looking at one mandate means looking at one mandate, drafts included."""
    with factory() as session:
        keep = _seed_angle(session, client_name="Arrakis", generated_at=_noon_utc(_TEST_DAY),
                           subject="ARRAKISIMPULS")
        _seed_angle(session, client_name="Zalando", generated_at=_noon_utc(_TEST_DAY),
                    subject="ZALANDOIMPULS")
        selected = keep.client_id

    body = client.get("/", params={"date": _TEST_DAY.isoformat(), "client": selected}).text

    assert "ARRAKISIMPULS" in body
    assert "ZALANDOIMPULS" not in body


def test_the_draft_cites_the_coverage_it_was_built_on(factory, client):
    """A draft without its sources cannot be checked, only believed."""
    with factory() as session:
        source_article = Article(
            title="BitMEX stellt den Betrieb ein",
            url="https://cash.at/bitmex",
            source="cash.at",
            published_at=_noon_utc(_TEST_DAY),
            fetched_at=_noon_utc(_TEST_DAY),
            summary_text="Der Handelsplatz kündigt die Abwicklung an.",
            language="de",
            title_hash="bitmex01",
        )
        session.add(source_article)
        session.flush()
        _seed_angle(
            session,
            generated_at=_noon_utc(_TEST_DAY),
            article_ids=[source_article.id],
        )

    body = client.get("/", params={"date": _TEST_DAY.isoformat()}).text

    column = body.split('class="anglecol"', 1)[1]
    assert "BitMEX stellt den Betrieb ein" in column
    assert 'href="https://cash.at/bitmex"' in column


def test_a_draft_citing_a_vanished_article_still_renders(factory, client):
    """Degrade to a draft without citations rather than to a 500."""
    with factory() as session:
        _seed_angle(session, generated_at=_noon_utc(_TEST_DAY), article_ids=[9999])

    resp = client.get("/", params={"date": _TEST_DAY.isoformat()})

    assert resp.status_code == 200
    assert "Börsenschließungen" in resp.text


def test_the_column_says_so_when_there_is_no_opening(factory, client):
    """An empty column is the normal case and must read as a working tool."""
    body = client.get("/", params={"date": _TEST_DAY.isoformat()}).text

    assert 'class="anglecol"' in body
    assert "Kein Anlass heute." in body
