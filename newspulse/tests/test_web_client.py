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
import re

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
            is_relevant=relevance >= 1, relevance_score=relevance,
            importance_score=importance,
            is_alert=is_alert,
        )
    )


def _headline_count(body: str) -> int:
    """How many archive rows the page rendered (one anchor class per row)."""
    return body.count("feed-item__headline")


# A month per calendar quarter so the multi-month archive spans a real range.
_JAN = dt.date(2026, 1, 15)


def _counts(fragment: str) -> list[str]:
    """The three numbers in one portfolio row, in column order.

    The row is a grid of `<span class="pcount ...">`, so reading the cells is
    the honest assertion: matching a rendered string like "<b>1</b> heute" tied
    every count test to one particular piece of markup and broke all of them the
    day the labels moved into a header row.
    """
    return re.findall(r'<span class="pcount[^"]*">(\d+)</span>', fragment)


def _content(body: str) -> str:
    """The page's own content, without the shared chrome.

    The sidebar lists every mandate on every page by design, so an assertion
    about what a *page* offers has to exclude it or it is really asserting that
    the navigation is empty.
    """
    return body.split('<main class="content">', 1)[-1].split("</main>", 1)[0]

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
    # The row carries the three numbers a morning decision needs, in order:
    # what arrived, what is on fire, what is ready to send. The archive total is
    # deliberately not among them — a fact about the past, given the emphasis of
    # a decision. The labels are a header row printed once, so they are asserted
    # against the table rather than against every mandate.
    # Scoped to the content: the sidebar names every mandate too, so splitting
    # the whole document on "Alpha AG" lands in the navigation, not the table.
    table = _content(body)
    alpha_row = table.split("Alpha AG", 1)[1].split("Beta AG", 1)[0]
    assert _counts(alpha_row) == ["1", "0", "0"]
    beta_row = table.split("Beta AG", 1)[1]
    assert _counts(beta_row) == ["0", "0", "0"]
    header = table.split('phead-row', 1)[1].split("</div>", 1)[0]
    for label in ("Mandant", "Heute", "Warnungen", "Impulse"):
        assert label in header


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
    # A dismissed match must not be counted anywhere on the row.
    assert _counts(_content(body).split("Alpha AG", 1)[1]) == ["0", "0", "0"]


# --- Per-client competitor sets ------------------------------------------------


def test_competitor_can_be_linked_and_unlinked_from_the_client_page(factory, client):
    with factory() as s:
        a = _seed_client(s, name="Alpha AG")
        b = _seed_client(s, name="Beta AG")
        s.commit()
        a_id, b_id = a.id, b.id

    client.post(f"/client/{a_id}/competitors", data={"competitor_id": b_id},
                follow_redirects=False)
    with factory() as s:
        assert [c.name for c in s.get(Client, a_id).competitors] == ["Beta AG"]

    client.post(f"/client/{a_id}/competitors/{b_id}/remove", follow_redirects=False)
    with factory() as s:
        assert s.get(Client, a_id).competitors == []
        # Removing a link must not touch the company itself.
        assert s.get(Client, b_id) is not None


def test_linking_is_one_directional(factory, client):
    """Benchmarking a mandate against a market leader must not add the mandate to
    the leader's own comparison set."""
    with factory() as s:
        a = _seed_client(s, name="Alpha AG")
        b = _seed_client(s, name="Beta AG")
        s.commit()
        a_id, b_id = a.id, b.id

    client.post(f"/client/{a_id}/competitors", data={"competitor_id": b_id},
                follow_redirects=False)
    with factory() as s:
        assert [c.name for c in s.get(Client, a_id).competitors] == ["Beta AG"]
        assert s.get(Client, b_id).competitors == []


def test_a_client_cannot_be_added_as_its_own_competitor(factory, client):
    """The schema forbids the self-link; the route must give a no-op, not a 500."""
    with factory() as s:
        a = _seed_client(s, name="Alpha AG")
        s.commit()
        a_id = a.id

    resp = client.post(f"/client/{a_id}/competitors", data={"competitor_id": a_id},
                       follow_redirects=False)
    assert resp.status_code == 303
    with factory() as s:
        assert s.get(Client, a_id).competitors == []


def test_share_of_voice_panel_renders_the_comparison(factory, client):
    with factory() as s:
        a = _seed_client(s, name="Alpha AG")
        b = _seed_client(s, name="Beta AG")
        s.flush()
        a.competitors.append(b)
        _seed_article(s, client_obj=a, title="A story", url="https://ex.de/a",
                      published_at=_local_noon(dt.date.today()))
        _seed_article(s, client_obj=b, title="B story", url="https://ex.de/b",
                      published_at=_local_noon(dt.date.today()))
        s.commit()
        a_id = a.id

    body = client.get(f"/client/{a_id}").text
    assert "Share of Voice" in body
    assert "Beta AG" in body
    assert "50.0%" in body


def test_without_competitors_the_panel_explains_rather_than_showing_100_percent(
    factory, client
):
    with factory() as s:
        a = _seed_client(s, name="Alpha AG")
        s.commit()
        a_id = a.id
    body = client.get(f"/client/{a_id}").text
    assert "Noch keine Wettbewerber hinterlegt" in body


def test_a_client_card_carries_no_nested_links(factory, client):
    """The card is one anchor. HTML forbids nesting anchors, so a link inside it
    silently closes the card early and the rest of the content escapes it —
    which is exactly how the layout broke."""
    with factory() as s:
        a = _seed_client(s, name="Alpha AG")
        b = _seed_client(s, name="Beta AG")
        s.flush()
        a.competitors.append(b)
        s.commit()

    body = client.get("/clients").text
    card_start = body.index('class="pcard')
    card_end = body.index("</a>", card_start)
    card = body[card_start:card_end]
    assert "<a " not in card
    # Competitors and the exports live in the deep dive, not on the card.
    assert "/export.xlsx" not in card
    assert "Beta AG" not in card


def test_the_deep_dive_is_where_the_competitors_and_the_workspace_live(factory, client):
    with factory() as s:
        a = _seed_client(s, name="Alpha AG")
        b = _seed_client(s, name="Beta AG")
        s.flush()
        a.competitors.append(b)
        s.commit()
        a_id = a.id

    body = client.get(f"/client/{a_id}").text
    assert "Beta AG" in body                      # share of voice
    assert f"/client/{a_id}/advice" in body
    # The workbook is no longer a trailing link in the strip — a download is not
    # a place, and beside ten tabs it read as an eleventh that had slipped. It
    # sits on this page, which is the one holding the coverage it exports.
    tabs = body.split('class="subtabs"', 1)[1].split("</nav>", 1)[0]
    assert f"/client/{a_id}/export.xlsx" not in tabs
    assert f"/client/{a_id}/export.xlsx" in body
    # The Coverage Map moved into the Wettbewerb tab, where the question it
    # answers is actually asked; the workspace strip carries that instead.
    assert f"/client/{a_id}/wettbewerb" in body
    assert f"/client/{a_id}/profil" in body


# --- The competitor picker -----------------------------------------------------


def test_only_companies_marked_as_competitors_can_be_added(factory, client):
    """Regression: the picker offered every other company, mandates included.

    A beauty-tech startup was proposed as Zalando's benchmark. A mandate is work
    to be done and a competitor is a yardstick; adding one as the other puts an
    unrelated mention count into the share-of-voice table.
    """
    with factory() as session:
        subject = Client(name="Zalando", aliases=[], industry="Modehandel",
                         keywords=[], alert_topics=[])
        mandate = Client(name="IB-7 Beauty Tech GmbH", aliases=[],
                         industry="Modehandel", keywords=[], alert_topics=[])
        rival = Client(name="About You", aliases=[], industry="Modehandel",
                       keywords=[], alert_topics=[], is_competitor=True)
        session.add_all([subject, mandate, rival])
        session.commit()
        subject_id = subject.id

    body = client.get(f"/client/{subject_id}").text

    # Nowhere in the page's own content, not merely absent from one control:
    # the mandate is never a yardstick, whichever picker it would appear in.
    # The sidebar is excluded because listing every mandate is its whole job.
    content = _content(body)
    assert "About You" in content
    assert "IB-7 Beauty Tech GmbH" not in content


def test_with_no_competitor_anywhere_the_page_offers_the_manual_field(factory, client):
    """An empty comparison set is not a dead end. Typing a name creates the
    company as a monitored competitor and links it to this mandate — the path a
    consultant who knows the market reaches for first."""
    with factory() as session:
        subject = Client(name="Allein AG", aliases=[], industry="Modehandel",
                         keywords=[], alert_topics=[])
        session.add(subject)
        session.commit()
        subject_id = subject.id

    body = client.get(f"/client/{subject_id}").text

    assert "Wettbewerber hinzufügen" in body
    assert f'action="/client/{subject_id}/competitors/accept"' in body
    assert "nur hier, nicht bei den anderen" in body



def test_the_client_tab_leads_with_its_positioning_drafts(factory, client):
    """The tab is called Impulse and has to contain some.

    It used to be "Empfehlungen", which reads a client's own press — and a mandate
    nobody writes about yet has none, so the page was empty for exactly the client
    that needed it most. The drafts come from the market and do not.
    """
    import datetime as dt

    from newspulse.models import Angle

    with factory() as session:
        subject = Client(name="Arrakis", aliases=[], keywords=[], alert_topics=[])
        session.add(subject)
        session.flush()
        session.add(
            Angle(
                client_id=subject.id,
                generated_at=dt.datetime.now(dt.UTC),
                subject="Börsenschließungen: Liquidität als Infrastruktur",
                message="Zwei Absätze Text.",
                context="c",
                thesis="Der Markt konsolidiert.",
            )
        )
        session.commit()
        subject_id = subject.id

    body = client.get(f"/client/{subject_id}/advice").text

    assert "Börsenschließungen: Liquidität als Infrastruktur" in body
    assert "Zwei Absätze Text." in body
    # And the position offers the one thing you can do with it. The second panel
    # this replaced was called "Empfehlungen" and nobody could say how it differed
    # from the draft above it — it differed by not being sendable.
    assert "Personalisierte Nachricht erzeugen" in body
    assert "Empfehlungen" not in body



def test_a_running_draft_makes_the_page_fetch_its_own_result(factory, client):
    """The page promised "die Seite aktualisiert sich" and never did.

    The draft runs on a worker thread; nothing on the page ever asked whether it
    had finished, so the finished impulse showed up only if the reader thought to
    reload. The poller lives inside the section it replaces, so it disappears
    with the notice once the draft is stored — no timer left running.
    """
    from newspulse.web.routes import advisory

    with factory() as session:
        subject = Client(
            name="IB-7 Beauty Tech GmbH",
            aliases=[],
            keywords=["KI in der Kosmetik"],
            alert_topics=[],
        )
        session.add(subject)
        session.commit()
        subject_id = subject.id

    idle = client.get(f"/client/{subject_id}/advice").text
    assert "hx-target=\"#positioning\"" not in idle

    # Hold the guard the way a running draft does.
    assert advisory._drafting.acquire(blocking=False)
    try:
        busy = client.get(f"/client/{subject_id}/advice").text
    finally:
        advisory._drafting.release()

    assert 'hx-target="#positioning"' in busy
    assert 'hx-trigger="every 3s"' in busy
    assert f'hx-get="/client/{subject_id}/advice"' in busy


def test_one_publisher_is_one_choice_however_the_feed_spelled_it(factory, client):
    """Feeds capitalise as they please. "Finanzen.net" and "finanzen.net" were
    two entries in the filter, each showing part of the same outlet's clips, so
    a clipping list filtered to one silently dropped the rest."""
    with factory() as s:
        mandate = _seed_client(s, name="Alpha AG")
        for i, spelling in enumerate(
            ["Finanzen.net", "Finanzen.net", "finanzen.net", "FINANZEN.NET"]
        ):
            _seed_article(
                s, client_obj=mandate, title=f"Meldung {i}",
                url=f"https://ex.de/{i}", published_at=_local_noon(_JAN),
                source=spelling,
            )
        s.commit()

    body = client.get("/archive").text
    options = body.count('value="Finanzen.net"') + body.count('value="finanzen.net"') \
        + body.count('value="FINANZEN.NET"')
    assert options == 1, "one publisher, one entry in the dropdown"

    # And picking it finds every clip, whichever way the feed wrote the name.
    filtered = client.get("/archive", params={"source": "finanzen.net"}).text
    assert _headline_count(filtered) == 4


def test_a_portfolio_of_zeroes_says_why_it_is_empty(factory, client):
    """Seven mandates at 0/0/0 looks identical whether the news was quiet or the
    sweep stopped four days ago. /today explains the same silence in a banner;
    this is the screen somebody opens first and it explained nothing."""
    today = dt.datetime.now().astimezone().date()
    with factory() as s:
        mandate = _seed_client(s, name="Alpha AG")
        _seed_article(
            s, client_obj=mandate, title="Von vorgestern",
            url="https://ex.de/alt",
            published_at=_local_noon(today - dt.timedelta(days=2)),
        )
        s.commit()

    body = _content(client.get("/").text)

    assert "Zuletzt Berichterstattung am" in body
    assert (today - dt.timedelta(days=2)).strftime("%d.%m.%Y") in body


def test_a_portfolio_with_coverage_today_says_nothing_extra(factory, client):
    """The sentence is missing information, not decoration: on a normal morning
    the numbers speak for themselves."""
    today = dt.datetime.now().astimezone().date()
    with factory() as s:
        mandate = _seed_client(s, name="Alpha AG")
        _seed_article(
            s, client_obj=mandate, title="Von heute", url="https://ex.de/neu",
            published_at=_local_noon(today),
        )
        s.commit()

    body = _content(client.get("/").text)

    assert "Zuletzt Berichterstattung am" not in body


def test_a_benchmark_gets_no_workspace_and_no_strip_pointing_at_one(factory, client):
    """A yardstick is measured against, not reported to.

    The sweep has always known that — it skips ``is_competitor`` companies for
    the radar, the impulses, the profile refresh, the report and the themes — but
    the pages that render those things did not, so a competitor was offered the
    whole workspace and every generate button on it. Pressing one spent a model
    call writing a document for a company that will never receive one.

    What it keeps is the reason it is on file: its coverage, and the charts that
    compare it. Those read rather than generate.
    """
    with factory() as s:
        mandate = _seed_client(s, name="Alpha AG")
        rival = _seed_client(s, name="Beta AG")
        rival.is_competitor = True
        s.commit()
        mandate_id, rival_id = mandate.id, rival.id

    for tab in ("advice", "berichte", "profil", "guide", "kickoff", "ki"):
        assert client.get(f"/client/{rival_id}/{tab}").status_code == 404, tab
        assert client.get(f"/client/{mandate_id}/{tab}").status_code == 200, tab

    # The archive stays, and so does the comparison it exists for.
    page = client.get(f"/client/{rival_id}")
    assert page.status_code == 200
    assert client.get(f"/client/{rival_id}/wettbewerb").status_code == 200
    # And it carries no strip: every tab on it would now be a dead link.
    assert 'class="subtabs"' not in page.text
    assert "Vergleichsunternehmen" in page.text


def test_the_generate_buttons_refuse_a_benchmark_too(factory, client):
    """The render filter is not the write boundary.

    A tab left open while a company was marked as a competitor elsewhere posts a
    button the page would no longer draw, and every one of these spends a model
    call.
    """
    with factory() as s:
        rival = _seed_client(s, name="Beta AG")
        rival.is_competitor = True
        s.commit()
        rival_id = rival.id

    for path in (
        f"/client/{rival_id}/profil/fill",
        f"/client/{rival_id}/impulse",
        f"/client/{rival_id}/guide/vorschlag",
        f"/client/{rival_id}/berichte/erzeugen",
    ):
        assert client.post(path).status_code == 404, path
