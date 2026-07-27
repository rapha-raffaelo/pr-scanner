"""Route tests for the client detail / history view (NP-08).

These drive ``GET /client/{id}`` through FastAPI's TestClient against a seeded
in-memory SQLite database — interface-level, not the whole app stack. The
``get_db`` dependency is overridden to hand the route a session bound to the
fixture engine, so no real database file or daily job is involved. A multi-month
archive is seeded for one client and each filter is asserted to narrow the list,
and to compose, correctly.
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
from newspulse.web.routes.client import _PAGE_SIZE


def _local_noon(day: dt.date) -> dt.datetime:
    """Noon on ``day`` in the machine's local tz — comfortably inside the local
    day window the date filter computes, regardless of the runner's timezone."""
    local_tz = dt.datetime.now().astimezone().tzinfo
    return dt.datetime.combine(day, dt.time(12, 0), tzinfo=local_tz)


@pytest.fixture
def factory():
    """A sessionmaker bound to a fresh in-memory database with the schema built.

    StaticPool keeps every session on the same single connection so the seeded
    rows are visible to the route's session.
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


def _seed_client(session, name: str = "Alpha AG", **kwargs) -> Client:
    obj = Client(name=name, **kwargs)
    session.add(obj)
    session.flush()
    return obj


def _seed_article(
    session,
    *,
    client_obj: Client,
    title: str,
    url: str,
    published_at: dt.datetime,
    source: str = "Handelsblatt",
    summary: str = "Ein Satz Zusammenfassung.",
    category: Category = Category.PRODUKT,
    importance: int = 5,
    relevance: int = 5,
    is_alert: bool = False,
) -> None:
    article = Article(
        title=title,
        url=url,
        source=source,
        published_at=published_at,
        fetched_at=published_at,
        summary_text="Feed-Snippet.",
        language="de",
        title_hash=url[-16:],
    )
    session.add(article)
    session.flush()
    session.add(
        Analysis(
            article_id=article.id,
            client_id=client_obj.id,
            summary=summary,
            category=category,
            relevance_score=relevance,
            importance_score=importance,
            is_alert=is_alert,
        )
    )


def _headline_count(body: str) -> int:
    """How many archive rows the page rendered (one anchor class per row)."""
    return body.count("feed-item__headline")


# A month per calendar quarter so the multi-month archive spans a real range.
_JAN = dt.date(2026, 1, 15)
_FEB = dt.date(2026, 2, 15)
_MAR = dt.date(2026, 3, 15)
_APR = dt.date(2026, 4, 15)


def _seed_multi_month(session) -> Client:
    """One client with a four-month archive of varied source/category/text rows."""
    c = _seed_client(session)
    _seed_article(
        session, client_obj=c, title="JAN Produktstart bei Alpha",
        url="https://ex.de/jan-1", published_at=_local_noon(_JAN),
        source="FAZ", category=Category.PRODUKT,
        summary="Alpha bringt ein neues Produkt.",
    )
    _seed_article(
        session, client_obj=c, title="FEB Rueckruf erschuettert Alpha",
        url="https://ex.de/feb-1", published_at=_local_noon(_FEB),
        source="Spiegel", category=Category.KRISE, is_alert=True, importance=9,
        summary="Ein Rueckruf belastet den Konzern.",
    )
    _seed_article(
        session, client_obj=c, title="MAR Neuer Finanzvorstand",
        url="https://ex.de/mar-1", published_at=_local_noon(_MAR),
        source="Handelsblatt", category=Category.PERSONALIE,
        summary="Alpha holt einen neuen CFO.",
    )
    _seed_article(
        session, client_obj=c, title="APR Quartalszahlen ueber Erwartung",
        url="https://ex.de/apr-1", published_at=_local_noon(_APR),
        source="FAZ", category=Category.FINANZEN,
        summary="Die Zahlen liegen ueber der Prognose.",
    )
    session.commit()
    return c


def test_shows_profile_and_full_archive_newest_first(factory, client):
    """The page renders the client profile and the whole archive, newest first."""
    with factory() as s:
        c = _seed_multi_month(s)
        client_id = c.id

    body = client.get(f"/client/{client_id}").text

    assert "Alpha AG" in body  # profile header
    assert _headline_count(body) == 4  # the full archive
    # Newest (April) appears before oldest (January) in document order.
    assert body.index("APR Quartalszahlen") < body.index("JAN Produktstart")


def test_row_shows_the_today_field_set(factory, client):
    """Each row carries headline out-link, source, date, summary, category,
    importance and the alert flag (the same fields as the Today view)."""
    with factory() as s:
        c = _seed_client(s)
        _seed_article(
            s, client_obj=c, title="Delta Alert Story",
            url="https://ex.de/delta", published_at=_local_noon(_FEB),
            source="FAZ", category=Category.KRISE, importance=8, is_alert=True,
            summary="Delta steckt in der Krise.",
        )
        s.commit()
        client_id = c.id

    body = client.get(f"/client/{client_id}").text

    assert 'href="https://ex.de/delta"' in body  # headline out-link
    assert 'target="_blank"' in body
    assert "FAZ" in body  # source
    assert _local_noon(_FEB).strftime("%d.%m.%Y") in body  # date
    assert "Delta steckt in der Krise." in body  # summary
    assert "krise" in body  # category tag
    assert "8/10" in body  # importance
    assert "tag--alert" in body  # alert flag


def test_date_range_filter_narrows_to_month(factory, client):
    """A date range returns only articles published inside it."""
    with factory() as s:
        c = _seed_multi_month(s)
        client_id = c.id

    body = client.get(
        f"/client/{client_id}",
        params={"date_from": "2026-02-01", "date_to": "2026-02-28"},
    ).text

    assert "FEB Rueckruf" in body
    assert "JAN Produktstart" not in body
    assert "MAR Neuer Finanzvorstand" not in body
    assert "APR Quartalszahlen" not in body


def test_source_filter_narrows(factory, client):
    """A source filter returns only that source's articles."""
    with factory() as s:
        c = _seed_multi_month(s)
        client_id = c.id

    body = client.get(f"/client/{client_id}", params={"source": "Spiegel"}).text

    assert "FEB Rueckruf" in body  # the only Spiegel article
    assert "JAN Produktstart" not in body  # FAZ
    assert "MAR Neuer Finanzvorstand" not in body  # Handelsblatt


def test_category_filter_narrows(factory, client):
    """A category filter returns only that category's articles."""
    with factory() as s:
        c = _seed_multi_month(s)
        client_id = c.id

    body = client.get(f"/client/{client_id}", params={"category": "personalie"}).text

    assert "MAR Neuer Finanzvorstand" in body  # the only personalie article
    assert "JAN Produktstart" not in body
    assert "FEB Rueckruf" not in body


def test_search_narrows_over_headline_and_summary(factory, client):
    """Free-text search matches both the headline and the feed summary."""
    with factory() as s:
        c = _seed_client(s)
        _seed_article(
            s, client_obj=c, title="Einzigartiges Schlagwort im Titel",
            url="https://ex.de/title-hit", published_at=_local_noon(_JAN),
            summary="Nichts besonderes hier.",
        )
        _seed_article(
            s, client_obj=c, title="Ganz normale Schlagzeile",
            url="https://ex.de/summary-hit", published_at=_local_noon(_FEB),
            summary="Hier steht das Sonderwort versteckt.",
        )
        s.commit()
        client_id = c.id

    # A term only in a headline surfaces that row and not the other.
    title_body = client.get(f"/client/{client_id}", params={"q": "Schlagwort"}).text
    assert "title-hit" in title_body
    assert "summary-hit" not in title_body

    # A term only in a summary surfaces the other row.
    summary_body = client.get(f"/client/{client_id}", params={"q": "Sonderwort"}).text
    assert "summary-hit" in summary_body
    assert "title-hit" not in summary_body


def test_filters_compose(factory, client):
    """Source and category filters combine — the intersection, not the union."""
    with factory() as s:
        c = _seed_client(s)
        _seed_article(
            s, client_obj=c, title="FAZ Krise Story",
            url="https://ex.de/faz-krise", published_at=_local_noon(_JAN),
            source="FAZ", category=Category.KRISE,
        )
        _seed_article(
            s, client_obj=c, title="FAZ Produkt Story",
            url="https://ex.de/faz-produkt", published_at=_local_noon(_FEB),
            source="FAZ", category=Category.PRODUKT,
        )
        _seed_article(
            s, client_obj=c, title="Spiegel Krise Story",
            url="https://ex.de/spiegel-krise", published_at=_local_noon(_MAR),
            source="Spiegel", category=Category.KRISE,
        )
        s.commit()
        client_id = c.id

    body = client.get(
        f"/client/{client_id}", params={"source": "FAZ", "category": "krise"}
    ).text

    # Only the row matching BOTH filters survives; each filter alone keeps two.
    assert "faz-krise" in body
    assert "faz-produkt" not in body  # right source, wrong category
    assert "spiegel-krise" not in body  # right category, wrong source
    assert _headline_count(body) == 1


def test_date_range_and_search_filters_compose(factory, client):
    """A date range and a free-text search combine — only the row inside the
    range AND matching the term survives, though each filter alone keeps two.

    These two filters touch different tables (published_at on Article, the term
    on Article.title/Analysis.summary) and the date path uses the DST-aware local
    bounds, so their composition is worth asserting on its own."""
    with factory() as s:
        c = _seed_client(s)
        _seed_article(
            s, client_obj=c, title="Sonderwort im Februar",
            url="https://ex.de/feb-match", published_at=_local_noon(_FEB),
        )
        _seed_article(
            s, client_obj=c, title="Anderes Thema im Februar",
            url="https://ex.de/feb-nomatch", published_at=_local_noon(_FEB),
        )
        _seed_article(
            s, client_obj=c, title="Sonderwort im Maerz",
            url="https://ex.de/mar-match", published_at=_local_noon(_MAR),
        )
        s.commit()
        client_id = c.id

    body = client.get(
        f"/client/{client_id}",
        params={"date_from": "2026-02-01", "date_to": "2026-02-28", "q": "Sonderwort"},
    ).text

    # Only the February row matching the term survives the intersection.
    assert "feb-match" in body
    assert "feb-nomatch" not in body  # in range, wrong text
    assert "mar-match" not in body  # matches text, out of range
    assert _headline_count(body) == 1


def test_archive_paginates_at_named_page_size(factory, client):
    """An archive larger than a page renders one page at a time with navigation."""
    overflow = 5  # a handful past a full page, so page two is small and obvious
    with factory() as s:
        c = _seed_client(s)
        base_day = dt.date(2026, 1, 1)
        for i in range(_PAGE_SIZE + overflow):
            _seed_article(
                s, client_obj=c, title=f"Story {i:03d}",
                url=f"https://ex.de/story-{i:03d}",
                published_at=_local_noon(base_day + dt.timedelta(days=i)),
            )
        s.commit()
        client_id = c.id

    first = client.get(f"/client/{client_id}").text
    assert _headline_count(first) == _PAGE_SIZE  # not the whole archive at once
    assert "page=2" in first  # a next link exists

    second = client.get(f"/client/{client_id}", params={"page": 2}).text
    assert _headline_count(second) == overflow  # the remainder
    assert "page=1" in second  # a prev link back


def test_zero_relevance_row_is_excluded(factory, client):
    """A relevance_score=0 analysis (non-matching pair) is not in the archive."""
    with factory() as s:
        c = _seed_client(s)
        _seed_article(
            s, client_obj=c, title="NOISE Nicht relevant",
            url="https://ex.de/noise", published_at=_local_noon(_FEB),
            relevance=0,
        )
        s.commit()
        client_id = c.id

    body = client.get(f"/client/{client_id}").text
    assert "NOISE Nicht relevant" not in body
    assert "Keine Artikel" in body  # empty state


def test_unknown_client_returns_404(client):
    """A client id that does not exist returns 404, not a 500."""
    assert client.get("/client/9999").status_code == 404


# --- Mandanten overview (/clients) --------------------------------------------


def test_clients_index_lists_portfolio_with_counts(factory, client):
    """The Mandanten overview lists every client and counts today's coverage
    separately from the whole archive."""
    today = dt.datetime.now().astimezone().date()
    with factory() as s:
        alpha = _seed_client(s, name="Alpha AG", industry="Chemie")
        beta = _seed_client(s, name="Beta AG")
        _seed_article(
            s, client_obj=alpha, title="Heute A", url="https://ex.de/a-today",
            published_at=_local_noon(today),
        )
        _seed_article(
            s, client_obj=alpha, title="Alt A", url="https://ex.de/a-old",
            published_at=_local_noon(_JAN),
        )
        _seed_article(
            s, client_obj=beta, title="Alt B", url="https://ex.de/b-old",
            published_at=_local_noon(_JAN),
        )
        s.commit()

    body = client.get("/clients").text

    assert "Alpha AG" in body
    assert "Beta AG" in body
    assert "Chemie" in body
    # Alpha: 1 today of 2 archived; Beta: 0 today of 1 archived.
    alpha_card = body.split("Alpha AG", 1)[1].split("Beta AG", 1)[0]
    assert "<b>1</b> heute" in alpha_card
    assert "<b>2</b> im Archiv" in alpha_card


def test_clients_index_excludes_irrelevant_and_handles_empty(factory, client):
    """A relevance_score=0 pair is not counted, and no clients renders an empty
    state rather than an error."""
    assert "Noch keine Mandanten" in client.get("/clients").text

    with factory() as s:
        c = _seed_client(s, name="Alpha AG")
        _seed_article(
            s, client_obj=c, title="NOISE", url="https://ex.de/noise",
            published_at=_local_noon(_JAN), relevance=0,
        )
        s.commit()

    body = client.get("/clients").text
    card = body.split("Alpha AG", 1)[1]
    assert "<b>0</b> im Archiv" in card
