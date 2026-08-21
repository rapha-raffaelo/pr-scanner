"""Tests for the daily job orchestrator and its CLI (NP-06).

These drive :func:`newspulse.job.run` directly against a fresh in-memory SQLite
database — no network, no ``claude`` subprocess, no ASGI server. Feeds are supplied
as an injected ``fetch`` returning in-memory :class:`~newspulse.ingest.FeedItem`
objects (the "fixture feeds"), and the analyzer is a deterministic fake, so the whole
sweep is exercised end to end while staying fast and reproducible.

The load-bearing case is ``test_second_run_over_same_feeds_adds_nothing``: it runs
the job twice over identical feeds and asserts the second run stores zero new
articles or analyses, exercising the URL-unique + title-hash + (article_id,
client_id)-unique idempotency the story requires.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from newspulse import cli, job, notify
from newspulse.db import make_engine
from newspulse.feeds import Feed
from newspulse.ingest import FeedItem
from newspulse.models import Analysis, Article, Base, Category, Client, Run, RunStatus
from newspulse.schemas import Analysis as AnalysisSchema

# A fixed publication instant so nothing depends on the wall clock. Older than the
# runs' fixed "now" below, so the watermark logic is exercised deterministically.
_PUBLISHED = dt.datetime(2026, 7, 20, 8, 0, tzinfo=dt.UTC)
_NOW = dt.datetime(2026, 7, 20, 18, 0, tzinfo=dt.UTC)


# --- Fixtures / builders -------------------------------------------------------


@pytest.fixture
def session():
    """A session against a fresh in-memory SQLite database (schema from the models).

    Mirrors ``test_clients.py``: one connection, schema built from ``Base.metadata``,
    so the job and the assertions share the same in-memory database.
    """
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as sess:
        yield sess


def _add_client(session, name: str, **over) -> Client:
    client = Client(name=name, aliases=over.get("aliases", []))
    session.add(client)
    session.commit()
    return client


def _item(
    title: str,
    link: str,
    *,
    source: str = "Handelsblatt",
    summary: str = "Eine kurze Zusammenfassung.",
    published_at: dt.datetime | None = None,
    language: str = "de",
) -> FeedItem:
    return FeedItem(
        title=title,
        link=link,
        source=source,
        published_at=published_at or _PUBLISHED,
        summary=summary,
        language=language,
    )


def _fetch_from(mapping: dict[str, list[FeedItem]]) -> job.FetchFeed:
    """A ``fetch(url, since, ...)`` returning the mapped items, ignoring ``since``.

    Ignoring ``since`` is deliberate: a second run then re-fetches identical items, so
    idempotency is proven against the storage dedup + DB constraints rather than an
    advanced watermark that would trivially return nothing.
    """

    def _fetch(url, since, *, source=None, fetched_at=None, **_):
        return list(mapping.get(url, []))

    return _fetch


class _FakeAnalyzer:
    """Deterministic stand-in: one Analysis per article, fixed scores.

    Records each call so a test can assert it was (or wasn't) invoked.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[int, list[int]]] = []

    def analyze(self, client, articles):
        self.calls.append((client.id, [article.id for article in articles]))
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
                reasoning="fake reasoning",
            )
            for article in articles
        ]


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


# --- Happy path ----------------------------------------------------------------


def test_run_persists_articles_analyses_and_a_run_row(session):
    """A clean sweep stores one article, one analysis, and an ok ``runs`` row."""
    _add_client(session, "Alpha AG")
    feeds = [Feed(name="Handelsblatt", url="https://hb.de/rss")]
    items = {"https://hb.de/rss": [_item("Alpha AG eroeffnet Werk", "https://hb.de/a1")]}

    report = job.run(
        session, analyzer=_FakeAnalyzer(), feeds=feeds, fetch=_fetch_from(items), now=lambda: _NOW
    )

    assert report.status is RunStatus.OK
    assert report.new_articles == 1
    assert report.analyses_written == 1
    assert _count(session, Article) == 1
    assert _count(session, Analysis) == 1

    run_row = session.scalars(select(Run)).one()
    assert run_row.status is RunStatus.OK
    assert run_row.articles_found == 1
    assert run_row.started_at == _NOW
    assert run_row.finished_at == _NOW
    assert run_row.errors == []


def test_item_matching_two_clients_yields_one_article_and_two_analyses(session):
    """One story about two portfolio companies is one article with two analyses."""
    _add_client(session, "Alpha AG")
    _add_client(session, "Beta AG")
    feeds = [Feed(name="dpa", url="https://dpa.de/rss")]
    items = {
        "https://dpa.de/rss": [_item("Alpha AG und Beta AG fusionieren", "https://dpa.de/m")]
    }

    report = job.run(
        session, analyzer=_FakeAnalyzer(), feeds=feeds, fetch=_fetch_from(items), now=lambda: _NOW
    )

    assert _count(session, Article) == 1
    assert _count(session, Analysis) == 2
    assert report.new_articles == 1
    assert report.analyses_written == 2


# --- Idempotency (the load-bearing end-to-end case) ----------------------------


def test_second_run_over_same_feeds_adds_nothing(session):
    """Running twice over identical feeds stores zero new articles or analyses.

    Covers all three idempotency mechanisms at once: a duplicate URL, a near-duplicate
    title (dpa wire copy under two outlets), and the UNIQUE (article_id, client_id).
    """
    _add_client(session, "Alpha AG")
    _add_client(session, "Beta AG")
    feeds = [Feed(name="dpa1", url="https://a.de/rss"), Feed(name="dpa2", url="https://b.de/rss")]
    items = {
        "https://a.de/rss": [
            _item("Alpha AG und Beta AG fusionieren", "https://a.de/merge", source="Alpha-Quelle"),
        ],
        "https://b.de/rss": [
            # Same wire story, different outlet + URL -> collapses by title hash.
            _item("Alpha AG und Beta AG fusionieren", "https://b.de/merge", source="Beta-Quelle"),
            _item("Beta AG meldet Zahlen", "https://b.de/beta-zahlen"),
        ],
    }
    fetch = _fetch_from(items)

    run_one = job.run(session, analyzer=_FakeAnalyzer(), feeds=feeds, fetch=fetch, now=lambda: _NOW)
    articles_after_first = _count(session, Article)
    analyses_after_first = _count(session, Analysis)

    run_two = job.run(session, analyzer=_FakeAnalyzer(), feeds=feeds, fetch=fetch, now=lambda: _NOW)

    # The merge wire copy collapsed to one article; plus the Beta-Zahlen story = 2.
    assert articles_after_first == 2
    # merge -> Alpha + Beta analyses; Beta-Zahlen -> Beta = 3.
    assert analyses_after_first == 3
    assert run_one.new_articles == 2

    assert _count(session, Article) == articles_after_first
    assert _count(session, Analysis) == analyses_after_first
    assert run_two.new_articles == 0
    assert run_two.analyses_written == 0


def test_second_run_analyzer_is_not_asked_to_re_analyze(session):
    """On the second run every item is already stored, so the analyzer sees nothing."""
    _add_client(session, "Alpha AG")
    feeds = [Feed(name="f", url="https://f.de/rss")]
    items = {"https://f.de/rss": [_item("Alpha AG News", "https://f.de/a")]}
    fetch = _fetch_from(items)

    job.run(session, analyzer=_FakeAnalyzer(), feeds=feeds, fetch=fetch, now=lambda: _NOW)
    second_analyzer = _FakeAnalyzer()
    job.run(session, analyzer=second_analyzer, feeds=feeds, fetch=fetch, now=lambda: _NOW)

    assert second_analyzer.calls == []


def test_failed_analysis_batch_is_retried_on_a_later_run(session):
    """A transient analyzer outage stores the article but no analysis; the next run
    (analyzer healthy) re-analyses the stored story instead of dropping it forever."""
    _add_client(session, "Alpha AG")
    feeds = [Feed(name="f", url="u")]
    items = {"u": [_item("Alpha AG launcht neues Produkt", "https://x/a")]}

    class _Boom:
        def analyze(self, client, articles):
            raise RuntimeError("transient analyzer outage")

    first = job.run(session, analyzer=_Boom(), feeds=feeds, fetch=_fetch_from(items), now=lambda: _NOW)
    assert first.status is RunStatus.PARTIAL
    assert _count(session, Article) == 1
    assert _count(session, Analysis) == 0  # outage: stored, not yet analysed

    good = _FakeAnalyzer()
    job.run(session, analyzer=good, feeds=feeds, fetch=_fetch_from(items), now=lambda: _NOW)

    assert good.calls != []  # the stored-but-un-analysed article is re-analysed
    assert _count(session, Article) == 1  # still one article (idempotent)
    assert _count(session, Analysis) == 1


def test_collapsed_duplicate_still_analyses_a_distinct_client_match(session):
    """Two outlets run the same wire headline but each teaser names a *different*
    client. Dedup collapses them to one stored article; the client matched only via the
    dropped copy must still get an analysis against the stored story."""
    _add_client(session, "Alpha AG")
    _add_client(session, "Beta AG")
    feeds = [Feed(name="f", url="u")]
    items = {
        "u": [
            _item("Grosse Fusion erschuettert den Markt", "https://a/1", source="AAA", summary="Alpha AG expandiert."),
            _item("Grosse Fusion erschuettert den Markt", "https://b/2", source="BBB", summary="Beta AG meldet Rekord."),
        ]
    }
    analyzer = _FakeAnalyzer()

    job.run(session, analyzer=analyzer, feeds=feeds, fetch=_fetch_from(items), now=lambda: _NOW)

    assert _count(session, Article) == 1  # collapsed to a single stored article
    analysed_client_ids = {client_id for client_id, _ in analyzer.calls}
    all_client_ids = {c.id for c in session.scalars(select(Client)).all()}
    assert analysed_client_ids == all_client_ids  # both Alpha AND Beta got analysed
    assert _count(session, Analysis) == 2


# --- Fault isolation -----------------------------------------------------------


def test_failing_feed_is_isolated_logged_and_run_is_partial(session, caplog):
    """A raising feed is recorded in the run's errors; the good feed still processes."""
    _add_client(session, "Alpha AG")
    feeds = [Feed(name="good", url="https://good.de/rss"), Feed(name="bad", url="https://bad.de/rss")]
    good_items = {"https://good.de/rss": [_item("Alpha AG launcht", "https://good.de/a")]}

    def fetch(url, since, **_):
        if url == "https://bad.de/rss":
            raise RuntimeError("connection reset")
        return list(good_items.get(url, []))

    with caplog.at_level(logging.WARNING):
        report = job.run(session, analyzer=_FakeAnalyzer(), feeds=feeds, fetch=fetch, now=lambda: _NOW)

    assert report.status is RunStatus.PARTIAL
    assert report.feeds_ok == 1
    assert _count(session, Article) == 1  # the good feed was still processed
    run_row = session.scalars(select(Run)).one()
    assert run_row.status is RunStatus.PARTIAL
    assert any("bad" in message for message in run_row.errors)


def test_failing_analysis_batch_is_isolated_and_other_clients_persist(session):
    """One client's analyzer failure drops only its analyses; the other client's persist."""
    _add_client(session, "Alpha AG")
    beta = _add_client(session, "Beta AG")
    feeds = [Feed(name="f", url="https://f.de/rss")]
    items = {
        "https://f.de/rss": [
            _item("Alpha AG News", "https://f.de/a"),
            _item("Beta AG News", "https://f.de/b"),
        ]
    }

    class _PartlyFailing:
        def analyze(self, client, articles):
            if client.name == "Alpha AG":
                raise RuntimeError("analyzer boom")
            return [
                AnalysisSchema(
                    article_id=article.id,
                    client_id=client.id,
                    is_relevant=True,
                    summary="ok",
                    category=Category.SONSTIGES,
                    relevance_score=3,
                    importance_score=3,
                    is_alert=False,
                    reasoning="r",
                )
                for article in articles
            ]

    report = job.run(session, analyzer=_PartlyFailing(), feeds=feeds, fetch=_fetch_from(items), now=lambda: _NOW)

    assert report.status is RunStatus.PARTIAL
    # Both articles stored (persist happens before analysis); only Beta's analysis wrote.
    assert _count(session, Article) == 2
    analyses = session.scalars(select(Analysis)).all()
    assert len(analyses) == 1
    assert analyses[0].client_id == beta.id
    run_row = session.scalars(select(Run)).one()
    assert any("Alpha AG" in message for message in run_row.errors)


def test_a_top_level_failure_still_records_a_failed_run(session, monkeypatch):
    """If the sweep crashes mid-persist, a ``failed`` runs row is still written."""
    _add_client(session, "Alpha AG")
    feeds = [Feed(name="f", url="https://f.de/rss")]
    items = {"https://f.de/rss": [_item("Alpha AG News", "https://f.de/a")]}

    def boom(_session, _kept, _fetched_at):
        raise RuntimeError("disk full")

    monkeypatch.setattr(job, "_persist_articles", boom)

    report = job.run(session, analyzer=_FakeAnalyzer(), feeds=feeds, fetch=_fetch_from(items), now=lambda: _NOW)

    assert report.status is RunStatus.FAILED
    run_row = session.scalars(select(Run)).one()
    assert run_row.status is RunStatus.FAILED
    assert run_row.finished_at is not None
    assert any("disk full" in message for message in run_row.errors)


# --- "Since the last run" ------------------------------------------------------


def test_first_run_looks_back_the_configured_window(session):
    """With no prior OK run, ``since`` is the fixed lookback before now."""
    _add_client(session, "Alpha AG")
    seen: dict[str, dt.datetime] = {}

    def fetch(url, since, **_):
        seen["since"] = since
        return []

    job.run(session, analyzer=_FakeAnalyzer(), feeds=[Feed(name="f", url="u")], fetch=fetch, now=lambda: _NOW)

    assert seen["since"] == _NOW - job._FIRST_RUN_LOOKBACK


def test_since_uses_last_successful_run_started_at(session):
    """A prior OK run pins ``since`` to that run's ``started_at``."""
    _add_client(session, "Alpha AG")
    prior_start = dt.datetime(2026, 7, 19, 6, 0, tzinfo=dt.UTC)
    session.add(
        Run(
            started_at=prior_start,
            finished_at=dt.datetime(2026, 7, 19, 6, 5, tzinfo=dt.UTC),
            status=RunStatus.OK,
            articles_found=3,
            errors=[],
        )
    )
    session.commit()
    seen: dict[str, dt.datetime] = {}

    def fetch(url, since, **_):
        seen["since"] = since
        return []

    job.run(session, analyzer=_FakeAnalyzer(), feeds=[Feed(name="f", url="u")], fetch=fetch, now=lambda: _NOW)

    assert seen["since"] == prior_start


def test_partial_run_does_not_advance_the_watermark(session):
    """A ``partial`` run is not "successful", so ``since`` falls back to the lookback."""
    _add_client(session, "Alpha AG")
    session.add(
        Run(
            started_at=dt.datetime(2026, 7, 19, 6, 0, tzinfo=dt.UTC),
            finished_at=dt.datetime(2026, 7, 19, 6, 5, tzinfo=dt.UTC),
            status=RunStatus.PARTIAL,
            articles_found=1,
            errors=["a feed failed"],
        )
    )
    session.commit()
    seen: dict[str, dt.datetime] = {}

    def fetch(url, since, **_):
        seen["since"] = since
        return []

    job.run(session, analyzer=_FakeAnalyzer(), feeds=[Feed(name="f", url="u")], fetch=fetch, now=lambda: _NOW)

    assert seen["since"] == _NOW - job._FIRST_RUN_LOOKBACK


# --- Dry run -------------------------------------------------------------------


def test_dry_run_reports_counts_but_writes_nothing(session):
    """A dry run fetches/matches/dedups and reports counts without any writes."""
    _add_client(session, "Alpha AG")
    feeds = [Feed(name="f", url="https://f.de/rss")]
    items = {"https://f.de/rss": [_item("Alpha AG News", "https://f.de/a")]}
    analyzer = _FakeAnalyzer()

    report = job.run(
        session, analyzer=analyzer, feeds=feeds, fetch=_fetch_from(items), dry_run=True, now=lambda: _NOW
    )

    assert report.dry_run is True
    assert report.items_fetched == 1
    assert report.candidates == 1
    assert report.new_articles == 1
    assert report.analyses_written == 0
    assert analyzer.calls == []  # the analyzer is never invoked in a dry run
    assert _count(session, Article) == 0
    assert _count(session, Analysis) == 0
    assert _count(session, Run) == 0  # a dry run writes no runs row


def test_dry_run_survives_an_unexpected_error_and_reports_failed(session, monkeypatch):
    """An unexpected error inside a dry run is caught and reported as ``failed``, never
    raised as a raw traceback to the CLI. A dry run writes no runs row, so the report
    is the only record — it must still come back cleanly."""
    _add_client(session, "Alpha AG")

    def boom(_session):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(job, "_load_known", boom)

    report = job.run(
        session,
        analyzer=_FakeAnalyzer(),
        feeds=[Feed(name="f", url="u")],
        fetch=_fetch_from({"u": [_item("Alpha AG News", "https://f/a")]}),
        dry_run=True,
        now=lambda: _NOW,
    )

    assert report.status is RunStatus.FAILED
    assert report.dry_run is True
    assert any("db exploded" in message for message in report.errors)
    assert _count(session, Run) == 0  # a dry run still writes no runs row


# --- Logging setup -------------------------------------------------------------


def test_setup_logging_attaches_a_rotating_file_handler(tmp_path):
    """The package logger gets one rotating file handler, idempotently."""
    log_path = tmp_path / "logs" / "newspulse.log"
    logger = logging.getLogger("newspulse")
    baseline = list(logger.handlers)
    try:
        returned = job.setup_logging(log_path)
        rotating = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
        assert any(Path(h.baseFilename) == returned for h in rotating)
        assert log_path.parent.exists()  # parent dir created

        # A second call with the same path does not stack another handler.
        handler_count = len(logger.handlers)
        job.setup_logging(log_path)
        assert len(logger.handlers) == handler_count
    finally:
        for handler in list(logger.handlers):
            if handler not in baseline:
                logger.removeHandler(handler)
                handler.close()


# --- Post-run alert notification (NP-10 wiring) --------------------------------


class _AlertingAnalyzer:
    """Analyzer whose single verdict is a fired alert, to exercise the notify wiring."""

    def analyze(self, client, articles):
        return [
            AnalysisSchema(
                article_id=article.id,
                client_id=client.id,
                is_relevant=True,
                summary=f"{client.name}: {article.title}",
                category=Category.KRISE,
                relevance_score=9,
                importance_score=9,
                is_alert=True,
                reasoning="fake reasoning",
            )
            for article in articles
        ]


def test_completed_run_with_a_fired_alert_delivers_a_notification(session, monkeypatch):
    """A completed run that fired an alert invokes the (mocked) delivery channel.

    Exercises the full wiring end to end: run -> persist -> finalize -> notify ->
    collect_fired_alerts -> notify_alerts -> the (patched) desktop delivery boundary.
    """
    _add_client(session, "Alpha AG")
    feeds = [Feed(name="f", url="https://f.de/rss")]
    items = {"https://f.de/rss": [_item("Krise bei Alpha AG", "https://f.de/a")]}

    delivered: list = []
    monkeypatch.setenv("NEWSPULSE_NOTIFY_CHANNEL", "desktop")
    monkeypatch.setattr(notify, "_send_desktop", lambda summary: delivered.append(summary))

    job.run(
        session, analyzer=_AlertingAnalyzer(), feeds=feeds, fetch=_fetch_from(items), now=lambda: _NOW
    )

    assert len(delivered) == 1
    assert delivered[0].total == 1
    assert "Alpha AG" in delivered[0].desktop_message


def test_completed_run_with_no_alerts_delivers_nothing(session, monkeypatch):
    """A quiet run (no alerts) sends nothing even on an active channel — no '0 alerts' noise."""
    _add_client(session, "Alpha AG")
    feeds = [Feed(name="f", url="https://f.de/rss")]
    items = {"https://f.de/rss": [_item("Alpha AG News", "https://f.de/a")]}

    delivered: list = []
    monkeypatch.setenv("NEWSPULSE_NOTIFY_CHANNEL", "desktop")
    monkeypatch.setattr(notify, "_send_desktop", lambda summary: delivered.append(summary))

    # _FakeAnalyzer produces is_alert=False, so the run fires no alerts.
    job.run(
        session, analyzer=_FakeAnalyzer(), feeds=feeds, fetch=_fetch_from(items), now=lambda: _NOW
    )

    assert delivered == []


def test_notification_failure_does_not_fail_the_run(session, monkeypatch):
    """A raising delivery channel is isolated: the run still records an ok ``runs`` row."""
    _add_client(session, "Alpha AG")
    feeds = [Feed(name="f", url="https://f.de/rss")]
    items = {"https://f.de/rss": [_item("Krise bei Alpha AG", "https://f.de/a")]}

    def boom(_summary):
        raise RuntimeError("notifier exploded")

    monkeypatch.setenv("NEWSPULSE_NOTIFY_CHANNEL", "desktop")
    monkeypatch.setattr(notify, "_send_desktop", boom)

    report = job.run(
        session, analyzer=_AlertingAnalyzer(), feeds=feeds, fetch=_fetch_from(items), now=lambda: _NOW
    )

    assert report.status is RunStatus.OK  # notification failure never fails the run
    run_row = session.scalars(select(Run)).one()
    assert run_row.status is RunStatus.OK
    assert _count(session, Analysis) == 1  # the alert analysis is still persisted


# --- CLI -----------------------------------------------------------------------


def _stub_report(*, status=RunStatus.OK, dry_run=False) -> job.RunReport:
    return job.RunReport(
        status=status,
        feeds_total=1,
        feeds_ok=1,
        items_fetched=0,
        candidates=0,
        new_articles=0,
        analyses_written=0,
        errors=[],
        dry_run=dry_run,
    )


@contextlib.contextmanager
def _null_session():
    yield object()


def test_cli_run_dispatches_to_job_with_an_analyzer(monkeypatch, capsys):
    """``newspulse run`` builds an analyzer, calls the job, and exits 0 on ok."""
    captured: dict[str, object] = {}

    def fake_run(session, *, analyzer=None, dry_run=False, **_):
        captured["dry_run"] = dry_run
        captured["analyzer_is_none"] = analyzer is None
        return _stub_report(dry_run=dry_run)

    monkeypatch.setattr(job, "run", fake_run)
    monkeypatch.setattr(job, "setup_logging", lambda *a, **k: Path("x"))
    monkeypatch.setattr(cli, "get_session", _null_session)
    monkeypatch.setattr(cli, "get_analyzer", lambda: object())

    rc = cli.main(["run"])

    assert rc == 0
    assert captured["dry_run"] is False
    assert captured["analyzer_is_none"] is False
    assert "status=ok" in capsys.readouterr().out


def test_cli_dry_run_flag_skips_building_the_analyzer(monkeypatch):
    """``newspulse run --dry-run`` passes dry_run and never constructs an analyzer."""
    captured: dict[str, object] = {}

    def fake_run(session, *, analyzer=None, dry_run=False, **_):
        captured["dry_run"] = dry_run
        captured["analyzer_is_none"] = analyzer is None
        return _stub_report(dry_run=True)

    built = {"analyzer": False}

    def fake_get_analyzer():
        built["analyzer"] = True
        return object()

    monkeypatch.setattr(job, "run", fake_run)
    monkeypatch.setattr(job, "setup_logging", lambda *a, **k: Path("x"))
    monkeypatch.setattr(cli, "get_session", _null_session)
    monkeypatch.setattr(cli, "get_analyzer", fake_get_analyzer)

    rc = cli.main(["run", "--dry-run"])

    assert rc == 0
    assert captured["dry_run"] is True
    assert captured["analyzer_is_none"] is True
    assert built["analyzer"] is False


def test_cli_returns_nonzero_exit_when_run_failed(monkeypatch):
    """A ``failed`` sweep exits non-zero so a scheduler notices."""
    monkeypatch.setattr(job, "run", lambda *a, **k: _stub_report(status=RunStatus.FAILED))
    monkeypatch.setattr(job, "setup_logging", lambda *a, **k: Path("x"))
    monkeypatch.setattr(cli, "get_session", _null_session)
    monkeypatch.setattr(cli, "get_analyzer", lambda: object())

    assert cli.main(["run"]) == 1


def test_cli_partial_run_still_exits_zero(monkeypatch):
    """A ``partial`` sweep did useful work, so it exits 0."""
    monkeypatch.setattr(job, "run", lambda *a, **k: _stub_report(status=RunStatus.PARTIAL))
    monkeypatch.setattr(job, "setup_logging", lambda *a, **k: Path("x"))
    monkeypatch.setattr(cli, "get_session", _null_session)
    monkeypatch.setattr(cli, "get_analyzer", lambda: object())

    assert cli.main(["run"]) == 0


# --- Per-client Google News queries in the sweep --------------------------------


def _seed_one_client(session, name="Alpha AG"):
    from newspulse.models import Client

    c = Client(name=name, aliases=[], industry=None, country="DE",
               keywords=[], alert_topics=[])
    session.add(c)
    session.commit()
    return c


def test_run_appends_a_client_search_feed_to_the_registry(session, monkeypatch):
    """The searches are additive: the curated registry still runs, with one query
    feed per active client on top."""
    from newspulse import config

    _seed_one_client(session)
    monkeypatch.setattr(config, "GOOGLE_NEWS_ENABLED", True)
    monkeypatch.setattr(job, "load_feeds", lambda: [Feed(name="Reg", url="https://reg.example/rss")])

    seen: list[tuple[str, bool]] = []

    def _fetch(url, since, *, source=None, fetched_at=None, per_entry_source=False, **_):
        seen.append((url, per_entry_source))
        return []

    job.run(session, analyzer=_FakeAnalyzer(), fetch=_fetch, now=lambda: _NOW)

    assert len(seen) == 2, seen
    registry, search = seen
    assert registry == ("https://reg.example/rss", False)
    assert "news.google.com/rss/search" in search[0]
    # The decisive flag: aggregator entries keep their own publisher.
    assert search[1] is True
    # The query carries the client, minus its legal form (see company_names).
    assert "Alpha" in search[0]
    assert "Alpha+AG" not in search[0] and "Alpha%20AG" not in search[0]


def test_google_news_can_be_switched_off(session, monkeypatch):
    from newspulse import config

    _seed_one_client(session)
    monkeypatch.setattr(config, "GOOGLE_NEWS_ENABLED", False)
    monkeypatch.setattr(job, "load_feeds", lambda: [Feed(name="Reg", url="https://reg.example/rss")])

    urls: list[str] = []

    def _fetch(url, since, **_):
        urls.append(url)
        return []

    job.run(session, analyzer=_FakeAnalyzer(), fetch=_fetch, now=lambda: _NOW)
    assert urls == ["https://reg.example/rss"]


def test_injected_feeds_are_never_augmented_with_searches(session, monkeypatch):
    """An explicit feed list is what a test is pinning down; silently adding a
    network-bound search feed to it would make such tests non-deterministic."""
    from newspulse import config

    _seed_one_client(session)
    monkeypatch.setattr(config, "GOOGLE_NEWS_ENABLED", True)

    urls: list[str] = []

    def _fetch(url, since, **_):
        urls.append(url)
        return []

    job.run(
        session,
        analyzer=_FakeAnalyzer(),
        feeds=[Feed(name="Only", url="https://only.example/rss")],
        fetch=_fetch,
        now=lambda: _NOW,
    )
    assert urls == ["https://only.example/rss"]


# --- The topic radar and the positioning drafts it feeds -----------------------
#
# This is the second track through the sweep, and its defining property is that the
# client is *not* named in the coverage: the term matcher would reject every item,
# correctly, so the pairing is carried from the feed that found it. The tests below
# pin exactly that, plus the two things that keep the column honest — nothing is
# analysed as coverage, and a failed draft never damages the run.


def _radar_client(session, name="Arrakis", **over):
    from newspulse.models import Client

    client = Client(
        name=name,
        aliases=[],
        industry=over.get("industry"),
        country="DE",
        keywords=over.get("keywords", ["Onchain-Liquidität"]),
        alert_topics=over.get("alert_topics", []),
        is_competitor=over.get("is_competitor", False),
    )
    session.add(client)
    session.commit()
    return client


def _enable_radar(monkeypatch, registry_url="https://reg.example/rss"):
    from newspulse import config

    monkeypatch.setattr(config, "GOOGLE_NEWS_ENABLED", True)
    monkeypatch.setattr(
        job, "load_feeds", lambda: [Feed(name="Reg", url=registry_url)]
    )


def _radar_fetch(radar_items: list[FeedItem]):
    """Fetch that answers the topic radar only, and nothing else."""

    def _fetch(url, since, *, source=None, fetched_at=None, per_entry_source=False, **_):
        if "Themen-Radar" in (source or ""):
            return list(radar_items)
        return []

    return _fetch


def test_radar_stores_coverage_that_names_no_client(session, monkeypatch):
    """The load-bearing case: the story that never mentions the mandate.

    "BitMEX stellt den Betrieb ein" is not about Arrakis and must not be filed as
    coverage of it — but it is exactly the development Arrakis can speak to, so it
    has to reach the archive and the draft.
    """
    from newspulse import angles

    _radar_client(session)
    _enable_radar(monkeypatch)
    # This test is about storage, not drafting. Without the stub the run reaches the
    # real `claude -p` — the drafting step swallows its failure, so the assertions
    # would still pass while the suite spends a minute in a subprocess.
    monkeypatch.setattr(angles, "suggest", lambda *a, **k: None)
    analyzer = _FakeAnalyzer()
    item = _item("BitMEX stellt den Betrieb ein", "https://cash.at/bitmex")

    report = job.run(
        session,
        analyzer=analyzer,
        fetch=_radar_fetch([item]),
        now=lambda: _NOW,
    )

    assert _count(session, Article) == 1
    # Stored, but never filed as coverage: no analysis pairs it with the client.
    assert _count(session, Analysis) == 0
    assert analyzer.calls == []
    assert report.items_fetched == 1


def test_a_competitor_gets_no_radar(session, monkeypatch):
    """Nobody writes a competitor a positioning message, so nothing is spent on one."""
    _radar_client(session, name="Otto", is_competitor=True)
    _enable_radar(monkeypatch)

    sources: list[str] = []

    def _fetch(url, since, *, source=None, **_):
        sources.append(source or "")
        return []

    job.run(session, analyzer=_FakeAnalyzer(), fetch=_fetch, now=lambda: _NOW)

    assert not any("Themen-Radar" in s for s in sources), sources


def test_a_client_without_themes_gets_no_radar(session, monkeypatch):
    """Without themes there is no way to tell which market coverage concerns them."""
    _radar_client(session, keywords=[], alert_topics=[])
    _enable_radar(monkeypatch)

    sources: list[str] = []

    def _fetch(url, since, *, source=None, **_):
        sources.append(source or "")
        return []

    job.run(session, analyzer=_FakeAnalyzer(), fetch=_fetch, now=lambda: _NOW)

    assert not any("Themen-Radar" in s for s in sources), sources


def test_radar_material_produces_a_stored_draft(session, monkeypatch):
    """One call per mandate with material, and the draft lands in the database."""
    from newspulse import angles
    from newspulse.models import Angle

    client = _radar_client(session)
    _enable_radar(monkeypatch)
    calls: list[int] = []

    def _fake_suggest(sess, cli, material, **_):
        calls.append(cli.id)
        draft = _draft_for(material)
        return draft

    monkeypatch.setattr(angles, "suggest", _fake_suggest)

    report = job.run(
        session,
        analyzer=_FakeAnalyzer(),
        fetch=_radar_fetch([_item("BitMEX stellt den Betrieb ein", "https://cash.at/x")]),
        now=lambda: _NOW,
    )

    assert calls == [client.id]
    assert report.angles_written == 1
    stored = session.scalars(select(Angle)).all()
    assert len(stored) == 1
    assert stored[0].client_id == client.id
    assert stored[0].article_ids  # always cites the coverage behind it


def _draft_for(material):
    """A minimal (draft, numbered) pair over the material the job resolved."""
    from newspulse import angles as angles_module
    from newspulse.schemas import AngleDraft

    numbered = angles_module.developments(material)
    draft = AngleDraft(
        worth_sending=True,
        subject="Liquidität als Infrastruktur",
        message="Zwei Absätze Text.",
        context="Mehrere Handelsplätze schließen.",
        thesis="Der Markt konsolidiert.",
        overclaim="Zentrale Börsen verschwinden.",
        statements=["Liquidität ist Infrastruktur."],
        evidence=[0],
        # Stamped, as the real generator stamps it: ``store`` refuses a draft
        # with no version rather than writing a NULL, which the interface
        # renders as "older than the recorded standards".
        brain_version=0,
    )
    return draft, numbered


def test_a_failing_draft_never_damages_the_run(session, monkeypatch):
    """A missed opportunity is not a broken sweep.

    The run must stay ``ok`` with its articles intact, and the failure must not
    surface as a feed error — the header would then report a fetch problem that
    never happened.
    """
    from newspulse import angles

    _radar_client(session)
    _enable_radar(monkeypatch)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("claude ist weg")

    monkeypatch.setattr(angles, "suggest", _boom)

    report = job.run(
        session,
        analyzer=_FakeAnalyzer(),
        fetch=_radar_fetch([_item("BitMEX stellt den Betrieb ein", "https://cash.at/y")]),
        now=lambda: _NOW,
    )

    assert report.status is RunStatus.OK
    assert report.angles_written == 0
    assert report.errors == []
    assert _count(session, Article) == 1


def test_one_client_failing_leaves_the_others_drafted(session, monkeypatch):
    """Per-client isolation: mandate B still gets its draft when A's call dies."""
    from newspulse import angles
    from newspulse.models import Angle

    first = _radar_client(session, name="Arrakis")
    second = _radar_client(session, name="Zalando", keywords=["Retouren"])
    _enable_radar(monkeypatch)

    def _suggest(sess, cli, material, **_):
        if cli.id == first.id:
            raise RuntimeError("erste Anfrage stirbt")
        return _draft_for(material)

    monkeypatch.setattr(angles, "suggest", _suggest)

    report = job.run(
        session,
        analyzer=_FakeAnalyzer(),
        fetch=_radar_fetch([_item("BitMEX stellt den Betrieb ein", "https://cash.at/z")]),
        now=lambda: _NOW,
    )

    assert report.angles_written == 1
    stored = session.scalars(select(Angle)).all()
    assert [a.client_id for a in stored] == [second.id]


def test_a_second_run_does_not_re_pitch_the_same_development(session, monkeypatch):
    """Yesterday's story is not today's opening.

    Dedup drops the already-stored item, so the second run has no fresh material
    and never asks — which is what keeps the column from repeating itself daily.
    """
    from newspulse import angles

    _radar_client(session)
    _enable_radar(monkeypatch)
    calls: list[int] = []

    def _suggest(sess, cli, material, **_):
        calls.append(cli.id)
        return _draft_for(material)

    monkeypatch.setattr(angles, "suggest", _suggest)
    fetch = _radar_fetch([_item("BitMEX stellt den Betrieb ein", "https://cash.at/rep")])

    job.run(session, analyzer=_FakeAnalyzer(), fetch=fetch, now=lambda: _NOW)
    job.run(session, analyzer=_FakeAnalyzer(), fetch=fetch, now=lambda: _NOW)

    assert len(calls) == 1, "the second run must not re-pitch a stored development"


def test_a_dry_run_spends_nothing_on_drafts(session, monkeypatch):
    """A preview reports what would be stored; a draft has nowhere to go."""
    from newspulse import angles

    _radar_client(session)
    _enable_radar(monkeypatch)
    monkeypatch.setattr(
        angles, "suggest", lambda *a, **k: pytest.fail("dry run must not draft")
    )

    sources: list[str] = []

    def _fetch(url, since, *, source=None, **_):
        sources.append(source or "")
        return []

    report = job.run(
        session, analyzer=_FakeAnalyzer(), fetch=_fetch, now=lambda: _NOW, dry_run=True
    )

    assert report.dry_run is True
    assert report.angles_written == 0
    assert not any("Themen-Radar" in s for s in sources), sources
