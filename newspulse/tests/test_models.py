"""Unit tests for the NewsPulse data model.

These exercise the ORM models directly against a throwaway SQLite database built
from ``Base.metadata`` (the same metadata the Alembic migration mirrors). They
assert the schema-level guarantees the story cares about: array round-trips, the
country default, the closed category enum, score ranges, and the UNIQUE
(article_id, client_id) that keeps one story = one article + N analyses.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from newspulse.db import make_engine
from newspulse.models import (
    DEFAULT_COUNTRY,
    Analysis,
    Article,
    Base,
    Category,
    Client,
    Run,
    RunStatus,
    Setting,
)


@pytest.fixture
def session():
    """A session against a fresh in-memory SQLite database."""
    from sqlalchemy.orm import sessionmaker

    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as sess:
        yield sess


def _make_article(**overrides) -> Article:
    now = dt.datetime(2026, 7, 24, 8, 0, tzinfo=dt.UTC)
    defaults = dict(
        title="Beispiel AG kündigt neues Produkt an",
        url="https://example.de/story-1",
        source="Handelsblatt",
        published_at=now,
        fetched_at=now,
        summary_text="Kurze Zusammenfassung aus dem Feed.",
        language="de",
        title_hash="abc123",
    )
    defaults.update(overrides)
    return Article(**defaults)


def test_client_array_fields_round_trip_intact(session):
    """Creating a client with array fields and reading them back preserves them."""
    client = Client(
        name="Beispiel AG",
        aliases=["Beispiel", "Beispiel Aktiengesellschaft"],
        industry="Automotive",
        keywords=["elektro", "batterie"],
        alert_topics=["Rückruf", "Insolvenz"],
    )
    session.add(client)
    session.commit()
    session.expire_all()

    loaded = session.get(Client, client.id)
    assert loaded.aliases == ["Beispiel", "Beispiel Aktiengesellschaft"]
    assert loaded.keywords == ["elektro", "batterie"]
    assert loaded.alert_topics == ["Rückruf", "Insolvenz"]


def test_client_country_defaults_to_de(session):
    """country is required and defaults to DE without being set explicitly."""
    client = Client(name="Ohne Land GmbH")
    session.add(client)
    session.commit()
    session.expire_all()

    assert session.get(Client, client.id).country == DEFAULT_COUNTRY == "DE"


def test_client_defaults_active_and_empty_arrays(session):
    client = Client(name="Neu GmbH")
    session.add(client)
    session.commit()
    session.expire_all()

    loaded = session.get(Client, client.id)
    assert loaded.active is True
    assert loaded.aliases == []
    assert loaded.keywords == []
    assert loaded.alert_topics == []
    # created_at is populated by the model's default; UTCDateTime re-attaches UTC
    # on read so it comes back tz-aware (see the round-trip test below).
    assert loaded.created_at is not None
    assert loaded.created_at.tzinfo is not None


def test_published_at_survives_store_read_and_compare(session):
    """Regression: datetimes go in tz-aware and must come back tz-aware so a
    later ``published_at >= cutoff`` comparison against a fresh _utcnow() cannot
    raise TypeError. SQLite discards tzinfo on write; UTCDateTime re-attaches it."""
    from newspulse.models import _utcnow

    # Relative to now, not a fixed calendar date: the assertion below compares
    # against a rolling `_utcnow() - 1 day` cutoff, so a hardcoded date silently
    # ages out and turns this into a failure that has nothing to do with tzinfo.
    stored_at = _utcnow().replace(microsecond=0)
    session.add(_make_article(published_at=stored_at))
    session.commit()
    session.expire_all()

    art = session.query(Article).one()
    assert art.published_at.tzinfo is not None
    assert art.published_at == stored_at
    # The comparison that previously crashed with naive-vs-aware TypeError.
    cutoff = _utcnow() - dt.timedelta(days=1)
    assert art.published_at >= cutoff


def test_naive_datetime_reads_back_as_utc_aware(session):
    """A naive datetime written to a UTCDateTime column is assumed UTC and reads
    back tz-aware, so downstream comparisons never mix naive and aware values."""
    naive = dt.datetime(2026, 7, 1, 12, 0)  # no tzinfo
    session.add(_make_article(published_at=naive))
    session.commit()
    session.expire_all()

    art = session.query(Article).one()
    assert art.published_at.tzinfo is not None
    assert art.published_at == naive.replace(tzinfo=dt.UTC)


def test_analysis_stores_category_enum_by_value(session):
    """The category column stores the StrEnum value ('produkt'), not its name."""
    client = Client(name="Beispiel AG")
    article = _make_article()
    session.add_all([client, article])
    session.flush()
    session.add(
        Analysis(is_relevant=True, 
            article_id=article.id,
            client_id=client.id,
            summary="Ein Satz.",
            category=Category.PRODUKT,
            relevance_score=8,
            importance_score=6,
            is_alert=False,
            reasoning="Direkt über das Kernprodukt.",
        )
    )
    session.commit()

    stored = session.execute(
        Analysis.__table__.select().with_only_columns(Analysis.__table__.c.category)
    ).scalar_one()
    assert stored == "produkt"


def test_category_enum_has_exactly_the_seven_values():
    assert [c.value for c in Category] == [
        "produkt",
        "personalie",
        "krise",
        "regulatorik",
        "finanzen",
        "wettbewerb",
        "sonstiges",
    ]


def test_one_article_two_clients_yields_two_analyses(session):
    """A single story about two portfolio companies is one article, two analyses."""
    a = Client(name="Alpha AG")
    b = Client(name="Beta AG")
    article = _make_article()
    session.add_all([a, b, article])
    session.flush()
    session.add_all(
        [
            Analysis(is_relevant=True, 
                article_id=article.id,
                client_id=a.id,
                category=Category.WETTBEWERB,
                relevance_score=7,
                importance_score=5,
                is_alert=False,
            ),
            Analysis(is_relevant=True, 
                article_id=article.id,
                client_id=b.id,
                category=Category.WETTBEWERB,
                relevance_score=6,
                importance_score=5,
                is_alert=False,
            ),
        ]
    )
    session.commit()

    assert session.query(Article).count() == 1
    assert session.query(Analysis).filter_by(article_id=article.id).count() == 2


def test_duplicate_article_client_pair_is_rejected(session):
    """The UNIQUE (article_id, client_id) forbids analysing one story twice per client."""
    client = Client(name="Beispiel AG")
    article = _make_article()
    session.add_all([client, article])
    session.flush()
    session.add(
        Analysis(is_relevant=True, 
            article_id=article.id,
            client_id=client.id,
            category=Category.PRODUKT,
            relevance_score=5,
            importance_score=5,
            is_alert=False,
        )
    )
    session.commit()

    session.add(
        Analysis(is_relevant=True, 
            article_id=article.id,
            client_id=client.id,
            category=Category.KRISE,
            relevance_score=9,
            importance_score=9,
            is_alert=True,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_article_url_is_unique(session):
    session.add(_make_article(url="https://dup.de/x", title_hash="h1"))
    session.commit()
    session.add(_make_article(url="https://dup.de/x", title_hash="h2"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_importance_score_out_of_range_is_rejected(session):
    """The 0..10 CHECK constraint rejects an out-of-range score."""
    client = Client(name="Beispiel AG")
    article = _make_article()
    session.add_all([client, article])
    session.flush()
    session.add(
        Analysis(is_relevant=True, 
            article_id=article.id,
            client_id=client.id,
            category=Category.FINANZEN,
            relevance_score=5,
            importance_score=11,  # out of range
            is_alert=False,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_articles_table_has_no_body_text_column():
    """Schema-level enforcement of the no-scrape rule: only summary_text exists."""
    columns = {c.name for c in Article.__table__.columns}
    assert "summary_text" in columns
    for forbidden in ("body", "body_text", "full_text", "content", "article_body"):
        assert forbidden not in columns


def test_articles_have_title_hash_index_and_unique_url():
    indexes = {ix.name for ix in Article.__table__.indexes}
    assert "ix_articles_title_hash" in indexes
    url_col = Article.__table__.c.url
    assert url_col.unique is True


def test_run_status_enum_values():
    assert [s.value for s in RunStatus] == ["ok", "partial", "failed"]


def test_run_round_trip_with_errors_list(session):
    run = Run(
        status=RunStatus.PARTIAL,
        articles_found=42,
        errors=["feed spiegel timed out", "batch 3 gave up"],
    )
    session.add(run)
    session.commit()
    session.expire_all()

    loaded = session.get(Run, run.id)
    assert loaded.status == RunStatus.PARTIAL
    assert loaded.articles_found == 42
    assert loaded.errors == ["feed spiegel timed out", "batch 3 gave up"]


def test_setting_key_value_round_trip(session):
    session.add(Setting(key="alert_threshold", value="8"))
    session.commit()
    assert session.get(Setting, "alert_threshold").value == "8"


def test_expected_tables_exist(session):
    tables = set(inspect(session.get_bind()).get_table_names())
    assert {"clients", "articles", "analyses", "runs", "settings"} <= tables
