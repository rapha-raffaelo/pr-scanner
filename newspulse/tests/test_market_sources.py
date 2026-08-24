"""The three market classes: fetched, parsed, and kept out of the news (SRC-01).

A study, a regulatory date and an event each break the shape of a news item in a
different place, and these tests pin down the places that matter:

* the date that makes an item *actionable* is stored, not only the date the sweep
  saw it — and for regulation that date is in the **future**, which nothing in the
  pipeline may treat as an error;
* a second sweep over the same sources stores nothing, on the URL and on the title
  for a source that re-issues its pages under new addresses;
* a signal never lands in ``articles``, so the numbers the agency is judged on do
  not move when the market sweep runs;
* a class whose source is unreachable is loud and alone: an ERROR, nothing stored
  for that class, and the other two plus the news sweep untouched.

No test performs a network call. The fixture payloads in ``tests/fixtures/`` are
answered at :func:`newspulse.ingest._fetch_raw` — the one HTTP call an ingest
makes — so the bytes still travel through the parser production uses.
"""

from __future__ import annotations

import datetime as dt
import logging
import urllib.error
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from newspulse import ingest, job, market_sources
from newspulse.db import make_engine
from newspulse.feeds import Feed
from newspulse.ingest import FeedItem
from newspulse.market_sources import (
    EventFetcher,
    MarketSource,
    RegulationFetcher,
    StudyFetcher,
)
from newspulse.models import (
    Analysis,
    Article,
    Base,
    Category,
    Client,
    MarketSignal,
    SignalKind,
    SignalOrigin,
    visible_coverage,
)
from newspulse.schemas import Analysis as AnalysisSchema

_FIXTURES = Path(__file__).parent / "fixtures"

# A fixed clock, before every effective date in the fixtures and after every
# publication date in them, so "in the future" is a property of the data rather
# than of the day the suite happens to run.
_NOW = dt.datetime(2026, 8, 24, 6, 10, tzinfo=dt.UTC)
_SINCE = _NOW - dt.timedelta(days=14)

_STUDIES_URL = "https://institut.example.de/rss"
_REGULATION_URL = "https://behoerde.example.de/rss"
_EVENTS_URL = "https://verband.example.de/rss"

_STUDY_SOURCE = MarketSource(
    name="Beispiel-Institut", url=_STUDIES_URL, kind=SignalKind.STUDIE
)
_REGULATION_SOURCE = MarketSource(
    name="Beispiel-Behoerde", url=_REGULATION_URL, kind=SignalKind.REGULIERUNG
)
_EVENT_SOURCE = MarketSource(
    name="Beispiel-Verband", url=_EVENTS_URL, kind=SignalKind.VERANSTALTUNG
)


# --- Fixtures / builders -------------------------------------------------------


@pytest.fixture
def session():
    """A session against a fresh in-memory database, schema from the models."""
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as sess:
        yield sess


@pytest.fixture
def serve(monkeypatch):
    """Answer the one HTTP call an ingest makes from a fixture file on disk.

    Mocking ``ingest._fetch_raw`` and nothing else keeps the network as the only
    stand-in: the fixture bytes go through the real feedparser path, the real date
    normalization and the real summary cleaning, so a change to any of those shows
    up here rather than being mocked past. A URL nobody mapped is unreachable,
    which is exactly what the fault-isolation tests need.
    """

    def _install(mapping: dict[str, str]) -> job.FetchFeed:
        payloads = {url: (_FIXTURES / name).read_bytes() for url, name in mapping.items()}

        def _fetch_raw(url: str, timeout: float) -> bytes:
            try:
                return payloads[url]
            except KeyError:
                raise urllib.error.URLError(f"unreachable in this test: {url}") from None

        monkeypatch.setattr(ingest, "_fetch_raw", _fetch_raw)
        return ingest.fetch_feed

    return _install


def _client(session, name="Arrakis Finance", *, industry=None, **over) -> Client:
    client = Client(
        name=name,
        aliases=[],
        industry=industry,
        keywords=over.get("keywords", []),
        alert_topics=over.get("alert_topics", []),
    )
    session.add(client)
    session.commit()
    return client


def _sweep(session, client, fetcher, fetch, *, seen=None) -> list[MarketSignal]:
    """One class, collected and stored, the way ``job._sweep_market`` does it."""
    drafts = fetcher.collect(client, since=_SINCE, now=_NOW)
    return market_sources.store(
        session,
        client,
        drafts,
        seen=seen if seen is not None else market_sources.already_seen(session, client),
        now=_NOW,
    )


def _signals(session, client, kind=None) -> list[MarketSignal]:
    stmt = select(MarketSignal).where(MarketSignal.client_id == client.id)
    if kind is not None:
        stmt = stmt.where(MarketSignal.kind == kind)
    return list(session.scalars(stmt.order_by(MarketSignal.id)).all())


def _by_title(signals, fragment: str) -> MarketSignal:
    matches = [s for s in signals if fragment in s.title]
    assert matches, f"no signal titled like {fragment!r} in {[s.title for s in signals]}"
    return matches[0]


# --- Each class is fetched, parsed and stored under its own kind ---------------


def test_each_class_is_stored_under_its_own_kind(session, serve):
    fetch = serve({
        _STUDIES_URL: "market_studies.xml",
        _REGULATION_URL: "market_regulation.xml",
        _EVENTS_URL: "market_events.xml",
    })
    client = _client(session)

    for fetcher_cls, source in (
        (StudyFetcher, _STUDY_SOURCE),
        (RegulationFetcher, _REGULATION_SOURCE),
        (EventFetcher, _EVENT_SOURCE),
    ):
        _sweep(session, client, fetcher_cls(fetch=fetch, sources=[source]), fetch)

    stored = {signal.kind for signal in _signals(session, client)}
    assert stored == {
        SignalKind.STUDIE,
        SignalKind.REGULIERUNG,
        SignalKind.VERANSTALTUNG,
    }
    assert len(_signals(session, client, SignalKind.STUDIE)) == 2
    assert len(_signals(session, client, SignalKind.REGULIERUNG)) == 3
    assert len(_signals(session, client, SignalKind.VERANSTALTUNG)) == 2


def test_a_study_carries_its_publication_date_and_not_only_the_date_it_was_found(
    session, serve
):
    """The actionable date for a study is when it was published — it stays citable
    for months, and ranking it by when a sweep happened to notice it says nothing."""
    fetch = serve({_STUDIES_URL: "market_studies.xml"})
    client = _client(session)

    _sweep(session, client, StudyFetcher(fetch=fetch, sources=[_STUDY_SOURCE]), fetch)

    study = _by_title(_signals(session, client), "Mittelstaendler")
    assert study.published_at == dt.datetime(2026, 8, 20, 6, 0, tzinfo=dt.UTC)
    assert study.found_at == _NOW
    assert study.published_at != study.found_at
    assert study.publisher == "Beispiel-Institut"


def test_a_regulatory_effective_date_is_stored_in_the_future_and_not_clamped(
    session, serve
):
    """The one property the regulatory class exists for. A rule that takes effect
    in January 2027 is worth having in August 2026 precisely because that date has
    not arrived; anything that clamped it to now would deliver it too late."""
    fetch = serve({_REGULATION_URL: "market_regulation.xml"})
    client = _client(session)

    _sweep(
        session, client, RegulationFetcher(fetch=fetch, sources=[_REGULATION_SOURCE]), fetch
    )

    session.expire_all()  # read it back out of the database, not out of the object
    item = _by_title(_signals(session, client), "Datenverordnung")
    assert item.effective_at == dt.datetime(2027, 1, 1, tzinfo=dt.UTC)
    assert item.effective_at > _NOW


def test_a_consultation_deadline_is_read_apart_from_the_date_the_rule_lands(
    session, serve
):
    """Two dates in one sentence that mean opposite things to a consultant: one is
    "you may still comment until", the other is "it now applies to you"."""
    fetch = serve({_REGULATION_URL: "market_regulation.xml"})
    client = _client(session)

    _sweep(
        session, client, RegulationFetcher(fetch=fetch, sources=[_REGULATION_SOURCE]), fetch
    )

    item = _by_title(_signals(session, client), "Datenverordnung")
    assert item.deadline_at == dt.datetime(2026, 9, 30, tzinfo=dt.UTC)
    assert item.effective_at == dt.datetime(2027, 1, 1, tzinfo=dt.UTC)


def test_a_rule_that_only_states_when_it_applies_carries_no_deadline(session, serve):
    fetch = serve({_REGULATION_URL: "market_regulation.xml"})
    client = _client(session)

    _sweep(
        session, client, RegulationFetcher(fetch=fetch, sources=[_REGULATION_SOURCE]), fetch
    )

    item = _by_title(_signals(session, client), "Meldepflicht")
    assert item.effective_at == dt.datetime(2027, 4, 1, tzinfo=dt.UTC)
    assert item.deadline_at is None


def test_an_item_that_states_no_date_stores_none_rather_than_guessing_one(
    session, serve
):
    """A calendar that invents a date is worse than one that admits it has none:
    the invented row sorts to the top and nobody can tell it was invented."""
    fetch = serve({_REGULATION_URL: "market_regulation.xml"})
    client = _client(session)

    _sweep(
        session, client, RegulationFetcher(fetch=fetch, sources=[_REGULATION_SOURCE]), fetch
    )

    item = _by_title(_signals(session, client), "Auslegungshinweise")
    assert item.effective_at is None
    assert item.deadline_at is None


def test_an_event_carries_its_own_date_and_its_speaker_deadline(session, serve):
    """The only class with a deadline, because a call for speakers closes."""
    fetch = serve({_EVENTS_URL: "market_events.xml"})
    client = _client(session)

    _sweep(session, client, EventFetcher(fetch=fetch, sources=[_EVENT_SOURCE]), fetch)

    item = _by_title(_signals(session, client), "Fachtagung")
    assert item.effective_at == dt.datetime(2027, 3, 12, tzinfo=dt.UTC)
    assert item.deadline_at == dt.datetime(2026, 11, 15, tzinfo=dt.UTC)


def test_a_written_german_month_with_an_umlaut_is_read_as_a_date():
    """Official German sources write "1. März 2027" as often as "01.03.2027", and a
    parser that only reads one of the two spellings silently empties the calendar."""
    effective, _deadline = RegulationFetcher(sources=[]).read_dates(
        "Die Verordnung tritt am 1. März 2027 in Kraft.", _NOW
    )

    assert effective == dt.datetime(2027, 3, 1, tzinfo=dt.UTC)


def test_an_impossible_date_is_dropped_rather_than_raised_on():
    """A typo in one line must not cost the item it appears in."""
    effective, _deadline = RegulationFetcher(sources=[]).read_dates(
        "Die Verordnung tritt am 31.02.2027 in Kraft.", _NOW
    )

    assert effective is None


# --- Running twice stores nothing new ------------------------------------------


def test_a_second_sweep_over_the_same_sources_stores_no_duplicate_signal(
    session, serve
):
    fetch = serve({
        _STUDIES_URL: "market_studies.xml",
        _REGULATION_URL: "market_regulation.xml",
        _EVENTS_URL: "market_events.xml",
    })
    client = _client(session)
    fetchers = [
        StudyFetcher(fetch=fetch, sources=[_STUDY_SOURCE]),
        RegulationFetcher(fetch=fetch, sources=[_REGULATION_SOURCE]),
        EventFetcher(fetch=fetch, sources=[_EVENT_SOURCE]),
    ]

    for _pass in range(2):
        seen = market_sources.already_seen(session, client)
        for fetcher in fetchers:
            _sweep(session, client, fetcher, fetch, seen=seen)

    assert len(_signals(session, client)) == 7


def test_a_source_that_reissued_its_pages_is_recognised_on_the_title(session, serve):
    """Official sources relaunch, and every URL changes at once. Without a title
    identity the whole calendar would arrive again as new signals the next morning,
    each of them a duplicate nobody could tell from a real item."""
    first = serve({_REGULATION_URL: "market_regulation.xml"})
    client = _client(session)
    _sweep(session, client, RegulationFetcher(fetch=first, sources=[_REGULATION_SOURCE]), first)
    before = len(_signals(session, client))

    moved = serve({_REGULATION_URL: "market_regulation_moved.xml"})
    _sweep(session, client, RegulationFetcher(fetch=moved, sources=[_REGULATION_SOURCE]), moved)

    assert before == 3
    assert len(_signals(session, client)) == 3


def test_a_study_already_in_the_clients_own_coverage_is_not_stored_twice(
    session, serve
):
    """A study a trade publication already reported is in ``articles`` under a
    headline. Listed again under Studien it would be the same document twice, with
    two different dates on it."""
    fetch = serve({_STUDIES_URL: "market_studies.xml"})
    client = _client(session)
    _cover(
        session,
        client,
        "Studie: Jeder dritte Mittelstaendler investiert in KI-Anwendungen",
        url="https://handelsblatt.example.de/ki-mittelstand",
    )

    _sweep(session, client, StudyFetcher(fetch=fetch, sources=[_STUDY_SOURCE]), fetch)

    titles = [signal.title for signal in _signals(session, client)]
    assert titles == ["Konjunkturerwartungen der Finanzmaerkte sinken im August deutlich"]


# --- The separation from coverage ----------------------------------------------


def _cover(session, client, title: str, *, url: str) -> Article:
    """One stored article with an analysis for ``client`` — the mandate's own press."""
    from newspulse.matching import title_hash

    article = Article(
        title=title,
        url=url,
        source="Handelsblatt",
        published_at=_NOW - dt.timedelta(days=2),
        fetched_at=_NOW,
        summary_text="Ein Satz.",
        language="de",
        title_hash=title_hash(title, "Handelsblatt"),
    )
    session.add(article)
    session.flush()
    session.add(
        Analysis(
            article_id=article.id,
            client_id=client.id,
            is_relevant=True,
            summary="s",
            category=Category.PRODUKT,
            relevance_score=6,
            importance_score=6,
            is_alert=False,
        )
    )
    session.commit()
    return article


def test_a_signal_is_never_written_into_articles(session, serve):
    fetch = serve({
        _STUDIES_URL: "market_studies.xml",
        _REGULATION_URL: "market_regulation.xml",
        _EVENTS_URL: "market_events.xml",
    })
    client = _client(session)

    for fetcher_cls, source in (
        (StudyFetcher, _STUDY_SOURCE),
        (RegulationFetcher, _REGULATION_SOURCE),
        (EventFetcher, _EVENT_SOURCE),
    ):
        _sweep(session, client, fetcher_cls(fetch=fetch, sources=[source]), fetch)

    assert session.scalar(select(func.count()).select_from(Article)) == 0
    assert len(_signals(session, client)) == 7


def test_the_coverage_queries_return_the_same_rows_before_and_after_a_market_sweep(
    session, serve
):
    """The number the agency is judged on must not move because a market sweep ran.

    Filing a consultation in ``articles`` would make every one of these queries
    wrong in a way nobody notices until a client report counts it as press.
    """
    fetch = serve({
        _STUDIES_URL: "market_studies.xml",
        _REGULATION_URL: "market_regulation.xml",
        _EVENTS_URL: "market_events.xml",
    })
    client = _client(session)
    _cover(session, client, "Arrakis Finance meldet Zahlen", url="https://hb.example.de/a1")

    coverage = select(Article.id, Article.title).join(Analysis).where(
        Analysis.client_id == client.id, visible_coverage()
    )
    before = session.execute(coverage).all()
    articles_before = session.execute(select(Article.id, Article.url)).all()

    for fetcher_cls, source in (
        (StudyFetcher, _STUDY_SOURCE),
        (RegulationFetcher, _REGULATION_SOURCE),
        (EventFetcher, _EVENT_SOURCE),
    ):
        _sweep(session, client, fetcher_cls(fetch=fetch, sources=[source]), fetch)

    assert session.execute(coverage).all() == before
    assert session.execute(select(Article.id, Article.url)).all() == articles_before
    assert len(_signals(session, client)) == 7


def test_signals_are_scoped_to_the_client_they_were_fetched_for(session, serve):
    """The separation the topic radar already enforces: one mandate's market never
    appears under another's, even when both read the same source."""
    fetch = serve({_REGULATION_URL: "market_regulation.xml"})
    one = _client(session, "Arrakis Finance")
    other = _client(session, "Caladan Logistik")

    _sweep(
        session, one, RegulationFetcher(fetch=fetch, sources=[_REGULATION_SOURCE]), fetch
    )

    assert len(_signals(session, one)) == 3
    assert _signals(session, other) == []


def test_the_same_item_is_a_real_signal_for_each_mandate_in_the_field(session, serve):
    """URL uniqueness is per client on purpose: a consultation matters to every
    mandate the rule applies to, and each has to be able to read and report it."""
    fetch = serve({_REGULATION_URL: "market_regulation.xml"})
    one = _client(session, "Arrakis Finance")
    other = _client(session, "Caladan Logistik")

    for client in (one, other):
        _sweep(
            session, client, RegulationFetcher(fetch=fetch, sources=[_REGULATION_SOURCE]), fetch
        )

    assert len(_signals(session, one)) == 3
    assert len(_signals(session, other)) == 3


# --- Provenance ----------------------------------------------------------------


def test_a_curated_source_is_stored_as_curated(session, serve):
    fetch = serve({_STUDIES_URL: "market_studies.xml"})
    client = _client(session)

    _sweep(session, client, StudyFetcher(fetch=fetch, sources=[_STUDY_SOURCE]), fetch)

    assert {s.origin for s in _signals(session, client)} == {SignalOrigin.KURATIERT}


def test_a_field_search_signal_records_that_it_came_from_a_search(session, serve):
    """The search half of DEC-1 B will return things that are not really studies, so
    a reader has to be able to judge a search-found row as one."""
    client = _client(session, industry="Onchain-Liquidität")
    fetcher = StudyFetcher(fetch=lambda *a, **k: [], sources=[])
    search = fetcher.sources_for(client)[-1]
    fetch = serve({search.url: "market_field_search.xml"})

    _sweep(session, client, StudyFetcher(fetch=fetch, sources=[]), fetch)

    stored = _signals(session, client)
    assert [s.origin for s in stored] == [SignalOrigin.SUCHE]
    # The aggregator names the real outlet per entry; crediting the search itself
    # would put a false publisher on every row.
    assert stored[0].publisher == "Boersenblatt"


def test_a_mandate_without_a_usable_field_gets_the_curated_sources_only(session):
    """A class query with no field is a bare "Studie" OR "Report", which returns the
    whole German press and calls it this client's market."""
    client = _client(session, industry=None)

    sources = StudyFetcher(sources=[_STUDY_SOURCE]).sources_for(client)

    assert [s.origin for s in sources] == [SignalOrigin.KURATIERT]


# --- Fault isolation, one guard per class --------------------------------------


def test_an_unreachable_class_logs_an_error_and_leaves_the_other_two_alone(
    session, serve, monkeypatch, caplog, no_market_sweep
):
    """The regulatory feed is dark; the studies and the events still arrive."""
    fetch = serve({_STUDIES_URL: "market_studies.xml", _EVENTS_URL: "market_events.xml"})
    monkeypatch.setattr(
        market_sources,
        "load_sources",
        lambda path=None: [_STUDY_SOURCE, _REGULATION_SOURCE, _EVENT_SOURCE],
    )
    client = _client(session)
    errors: list[str] = []

    with caplog.at_level(logging.ERROR, logger="newspulse.job"):
        written = no_market_sweep(session, [client], _SINCE, fetch, _NOW, errors)

    assert written == 4  # 2 studies + 2 events; nothing from the dark class
    assert _signals(session, client, SignalKind.REGULIERUNG) == []
    assert len(_signals(session, client, SignalKind.STUDIE)) == 2
    assert len(_signals(session, client, SignalKind.VERANSTALTUNG)) == 2
    assert [r.levelno for r in caplog.records] == [logging.ERROR]
    assert "regulierung" in caplog.records[0].getMessage()
    assert any("regulierung" in message for message in errors)


def test_a_competitor_gets_no_market_sweep(session, serve, monkeypatch, no_market_sweep):
    """A yardstick is tracked to compare its share of the conversation; nobody reads
    it a market page, so fetching one would spend the sweep on nothing."""
    fetch = serve({_STUDIES_URL: "market_studies.xml"})
    monkeypatch.setattr(market_sources, "load_sources", lambda path=None: [_STUDY_SOURCE])
    rival = _client(session, "Harkonnen AG")
    rival.is_competitor = True
    session.commit()

    written = no_market_sweep(session, [rival], _SINCE, fetch, _NOW, [])

    assert written == 0
    assert _signals(session, rival) == []


# --- The curated list, as data --------------------------------------------------


def test_the_shipped_source_list_is_well_formed_and_unique():
    """Structural only, and deliberately offline. A source that has moved announces
    itself at ERROR on the next sweep, which is the honest place for it."""
    sources = market_sources.load_sources()

    assert len(sources) >= 6, "the curated list should not silently shrink"
    assert {s.kind for s in sources} == set(SignalKind), "every class needs a source"
    urls = [s.url for s in sources]
    assert len(set(urls)) == len(urls), "duplicate source URL in the curated list"
    for source in sources:
        assert source.name.strip(), f"source with empty name: {source.url}"
        assert source.url.startswith("https://"), f"{source.name}: must be https"


def test_a_source_row_naming_an_unknown_class_is_skipped_not_fatal(tmp_path):
    """One typo must never take the other eleven sources down with it."""
    path = tmp_path / "sources.toml"
    path.write_text(
        '[[sources]]\nname = "Gut"\nurl = "https://a.example.de/rss"\nkind = "studie"\n'
        '[[sources]]\nname = "Tippfehler"\nurl = "https://b.example.de/rss"\n'
        'kind = "studien"\n',
        "utf-8",
    )

    sources = market_sources.load_sources(path)

    assert [s.name for s in sources] == ["Gut"]


# --- The news sweep is unaffected ----------------------------------------------


class _FakeAnalyzer:
    """One relevant analysis per article, so the news half of the sweep completes."""

    def analyze(self, client, articles):
        return [
            AnalysisSchema(
                article_id=article.id,
                client_id=client.id,
                is_relevant=True,
                summary=f"{client.name}: {article.title}",
                category=Category.PRODUKT,
                relevance_score=6,
                importance_score=6,
                is_alert=False,
                reasoning="fake",
            )
            for article in articles
        ]


def test_a_dark_market_class_does_not_disturb_the_daily_news_sweep(
    session, monkeypatch, no_market_sweep
):
    """The direction the story cares about most: market ingest is additional, and a
    market source going dark must not cost this morning's coverage."""
    monkeypatch.setattr(job, "_sweep_market", no_market_sweep)  # the real one
    monkeypatch.setattr(market_sources, "load_sources", lambda path=None: [_STUDY_SOURCE])
    monkeypatch.setattr(job.config, "GOOGLE_NEWS_ENABLED", False)
    _client(session, "Alpha AG")  # the sweep needs a mandate; nothing reads it back
    news = {
        "https://hb.example.de/rss": [
            FeedItem(
                title="Alpha AG eroeffnet Werk in Bremen",
                link="https://hb.example.de/a1",
                source="Handelsblatt",
                published_at=_NOW - dt.timedelta(days=1),
                summary="Eine kurze Zusammenfassung des Vorgangs.",
                language="de",
            )
        ]
    }

    def _fetch(url, since, *, source=None, fetched_at=None, **_):
        if url == _STUDIES_URL:
            raise urllib.error.URLError("the institute is down")
        return list(news.get(url, []))

    report = job.run(
        session,
        analyzer=_FakeAnalyzer(),
        feeds=[Feed(name="Handelsblatt", url="https://hb.example.de/rss")],
        fetch=_fetch,
        now=lambda: _NOW,
    )

    assert report.new_articles == 1
    assert report.analyses_written == 1
    assert report.signals_written == 0
    assert session.scalar(select(func.count()).select_from(MarketSignal)) == 0


def test_the_daily_sweep_stores_the_market_classes_it_fetched(
    session, serve, monkeypatch, no_market_sweep
):
    fetch_market = serve({_STUDIES_URL: "market_studies.xml"})
    monkeypatch.setattr(job, "_sweep_market", no_market_sweep)  # the real one
    monkeypatch.setattr(market_sources, "load_sources", lambda path=None: [_STUDY_SOURCE])
    monkeypatch.setattr(job.config, "GOOGLE_NEWS_ENABLED", False)
    client = _client(session, "Alpha AG")

    def _fetch(url, since, *, source=None, fetched_at=None, **kwargs):
        if url == _STUDIES_URL:
            return fetch_market(url, since, source=source, fetched_at=fetched_at, **kwargs)
        return []

    report = job.run(
        session,
        analyzer=_FakeAnalyzer(),
        feeds=[Feed(name="Handelsblatt", url="https://hb.example.de/rss")],
        fetch=_fetch,
        now=lambda: _NOW,
    )

    assert report.signals_written == 2
    assert len(_signals(session, client, SignalKind.STUDIE)) == 2
    assert report.new_articles == 0
