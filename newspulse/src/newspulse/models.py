"""SQLAlchemy ORM models for NewsPulse.

The data is deliberately relational (clients -> articles -> analyses with foreign
keys) and designed to grow: ``country`` lives on the client from day one so a
second country is a config change, not a migration. The schema also encodes two
non-negotiable product constraints:

* There is no full-body-text column anywhere. ``articles`` stores only the
  feed-provided ``summary_text`` snippet, which makes the no-scrape rule
  (Leistungsschutzrecht) a schema-level guarantee rather than a convention.
* A story is stored once as an ``articles`` row and analysed once per client via
  ``analyses``, enforced by a UNIQUE (article_id, client_id) — one story about
  two portfolio companies is one article with two analyses, never a duplicate.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy import Column, Table
from sqlalchemy import Enum as SAEnum
from sqlalchemy import JSON
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)
from sqlalchemy.types import TypeDecorator

# Country stored on every client so the schema supports future countries without
# a migration to add the column. Germany-only for the POC.
DEFAULT_COUNTRY = "DE"

# Scores are integers on a fixed 0..10 scale; enforced by a CHECK constraint so a
# bad analyzer response can never persist an out-of-range value.
SCORE_MIN = 0
SCORE_MAX = 10

# DB-level DEFAULT for the JSON array columns. The ORM default (``default=list``)
# only fires for inserts that go through SQLAlchemy; this makes the empty array a
# schema guarantee so a raw ``INSERT`` that omits the column can't violate NOT
# NULL. "[]" is valid JSON that SQLite stores verbatim.
_EMPTY_JSON_ARRAY = "[]"


class TriageState(StrEnum):
    """Where one piece of coverage stands in the operator's morning workflow.

    Without this the list is identical every time it is opened, so a 25-article
    day means re-reading yesterday to find what is new. State is per
    ``(article, client)`` — the same story can be handled for one mandate and
    still open for another.
    """

    NEU = "neu"
    GELESEN = "gelesen"
    ERLEDIGT = "erledigt"
    MARKIERT = "markiert"  # flagged to raise with the client


class Tonality(StrEnum):
    """How a story reads *for the client* — not how neutral the article is.

    The distinction is the whole point. "Zalando schließt Standort: 2.100 Jobs
    weg" is a neutrally written news report; general sentiment analysis scores it
    neutral. For the client's comms team it is unambiguously negative — it is the
    story a defence is built around. Tone here is always from the mandate's
    perspective.

    UNBEKANNT exists because this field arrived after the archive did: every
    analysis written before it is honestly unknown rather than falsely neutral.
    """

    POSITIV = "positiv"
    NEUTRAL = "neutral"
    NEGATIV = "negativ"
    UNBEKANNT = "unbekannt"


class Category(StrEnum):
    """The exact set of story categories. StrEnum so the value ('produkt') is what
    is stored and compared, and the set is closed — no stray string literals."""

    PRODUKT = "produkt"
    PERSONALIE = "personalie"
    KRISE = "krise"
    REGULATORIK = "regulatorik"
    FINANZEN = "finanzen"
    WETTBEWERB = "wettbewerb"
    SONSTIGES = "sonstiges"


class RunStatus(StrEnum):
    """Outcome of a daily sweep. Kept typed so the dashboard and job code share one
    closed value set rather than passing raw strings around."""

    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"


def _utcnow() -> dt.datetime:
    """Timezone-aware UTC now, used as a Python-side column default."""
    return dt.datetime.now(dt.UTC)


class UTCDateTime(TypeDecorator):
    """A ``DateTime(timezone=True)`` that always hands back a tz-aware UTC value.

    SQLite has no native datetime type: it discards ``tzinfo`` on write, so a
    value stored as tz-aware ``dt.datetime.now(dt.UTC)`` reads back *naive*. That
    breaks the very next feature — any ``published_at >= cutoff`` comparison
    against a fresh ``_utcnow()`` raises ``TypeError: can't compare offset-naive
    and offset-aware datetimes``. This decorator normalizes both directions:
    everything is stored as UTC, and every read re-attaches ``dt.UTC`` so the
    application never sees a naive datetime. ``impl`` stays exactly
    ``DateTime(timezone=True)`` so the schema Alembic emits is unchanged.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(
        self, value: dt.datetime | None, dialect: object
    ) -> dt.datetime | None:
        if value is None:
            return None
        # A naive value is assumed to already be UTC; an aware one is converted.
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.UTC)
        return value.astimezone(dt.UTC)

    def process_result_value(
        self, value: dt.datetime | None, dialect: object
    ) -> dt.datetime | None:
        if value is None:
            return None
        # SQLite drops tzinfo on write; re-attach UTC so reads are always aware.
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.UTC)
        return value.astimezone(dt.UTC)


class Base(DeclarativeBase):
    # Map Python list[str] array-ish fields onto SQLite JSON columns.
    type_annotation_map = {
        list[str]: JSON,
    }


# --- Per-client competitor sets -------------------------------------------------
#
# A competitor set belongs to a *client*, not to the portfolio: Zalando competes
# with About You and Otto, Siemens with ABB. A portfolio-wide flag cannot express
# that, and a share-of-voice number computed across unrelated industries is
# meaningless — "Zalando vs Siemens" is not a market.
#
# Modelled as a relation rather than a JSON list of ids on the client. The other
# JSON columns here (aliases, keywords, alert_topics) hold *values*; these are
# *references* to other rows, and a reference the database cannot check is one
# that will eventually dangle. The FKs cascade, so removing a company removes its
# links with it.
#
# Both directions are stored explicitly rather than inferred, so "X competes with
# Y" does not silently imply the reverse — an agency may benchmark a mandate
# against a market leader without wanting the leader's own page to list it.
client_competitors = Table(
    "client_competitors",
    Base.metadata,
    Column(
        "client_id",
        Integer,
        ForeignKey("clients.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "competitor_id",
        Integer,
        ForeignKey("clients.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    # A company is not its own competitor; the check makes that a schema
    # guarantee rather than something every caller has to remember.
    CheckConstraint("client_id != competitor_id", name="ck_client_competitors_distinct"),
)


class Client(Base):
    """A tracked portfolio company."""

    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    aliases: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON),
        default=list,
        nullable=False,
        server_default=_EMPTY_JSON_ARRAY,
    )
    industry: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # An optional logo. Stored as a URL rather than a blob: the dashboard is the
    # only consumer, and a client's own CDN logo is always more current than a
    # copy. Empty is the normal case — a generated monogram stands in, so the
    # portfolio never looks half-configured.
    logo_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    # Required, defaults to DE (both Python-side and in the DB) so existing rows
    # and inserts that omit it always carry a country.
    country: Mapped[str] = mapped_column(
        String(2), nullable=False, default=DEFAULT_COUNTRY, server_default=DEFAULT_COUNTRY
    )
    keywords: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON),
        default=list,
        nullable=False,
        server_default=_EMPTY_JSON_ARRAY,
    )
    alert_topics: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON),
        default=list,
        nullable=False,
        server_default=_EMPTY_JSON_ARRAY,
    )
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )
    # A competitor is monitored exactly like a mandate but never reported *to* —
    # it exists to answer "how much of the conversation did we own this month".
    # Modelled as a flag rather than a separate table because everything a
    # competitor needs (aliases, keywords, matching, archive) is already here.
    is_competitor: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )

    # The companies this client is benchmarked against. Each is itself a Client
    # row, so a competitor is matched, analysed and archived exactly like a
    # mandate — which is what makes its mention count comparable at all.
    competitors: Mapped[list["Client"]] = relationship(
        "Client",
        secondary=client_competitors,
        primaryjoin=lambda: Client.id == client_competitors.c.client_id,
        secondaryjoin=lambda: Client.id == client_competitors.c.competitor_id,
        lazy="selectin",
    )

    analyses: Mapped[list["Analysis"]] = relationship(
        back_populates="client", cascade="all, delete-orphan"
    )


class Article(Base):
    """One deduped feed story. Not per client.

    Only feed-syndicated fields plus a normalized-title hash for dedup. No
    full-body-text column exists (Leistungsschutzrecht / no-scrape rule).
    """

    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    # UNIQUE so re-ingesting the same link never creates a duplicate row.
    url: Mapped[str] = mapped_column(String(2048), nullable=False, unique=True)
    source: Mapped[str] = mapped_column(String(255), nullable=False)
    published_at: Mapped[dt.datetime] = mapped_column(UTCDateTime(), nullable=False)
    fetched_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )
    # The feed-provided snippet only. This is the *only* body-ish text stored.
    summary_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # Hash of the normalized title, indexed, so near-duplicate wire copy across
    # outlets collapses to one stored story.
    title_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # The byline, when the feed supplies one. Nullable because most German feeds
    # omit it — knowing *which journalist* covers a client repeatedly is how a
    # media list gets built, and it was previously discarded at ingest.
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)

    analyses: Mapped[list["Analysis"]] = relationship(
        back_populates="article", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_articles_title_hash", "title_hash"),
        # Every day-scoped read filters on published_at (Today's local-day
        # window, the archive date range, the per-client counts), so it carries
        # the same weight as the dedup hash as the archive grows.
        Index("ix_articles_published_at", "published_at"),
    )


class Analysis(Base):
    """One per (article, client) pair. Claude's verdict on whether this story
    concerns this client, plus its classification and scores."""

    __tablename__ = "analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), nullable=False
    )
    # Indexed on its own: the composite UNIQUE (article_id, client_id) can't serve
    # a client_id-only filter (leftmost-prefix rule), and "all analyses for a
    # client" is the primary dashboard access pattern.
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The analyzer's primary relevance judgment (schemas.Analysis.is_relevant).
    # Recomputed/returned in code and stored so the DB can answer "was this article
    # relevant to this client?" without re-running analysis. Defaults false so a
    # raw INSERT that omits it is a safe non-relevant row.
    is_relevant: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[Category] = mapped_column(
        SAEnum(
            Category,
            values_callable=lambda enum: [m.value for m in enum],
            # Emit a DB-level CHECK (category IN (...)) so a raw INSERT can't store
            # a value outside the closed set — consistent with the score CHECKs.
            create_constraint=True,
        ),
        nullable=False,
    )
    relevance_score: Mapped[int] = mapped_column(Integer, nullable=False)
    importance_score: Mapped[int] = mapped_column(Integer, nullable=False)
    is_alert: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    # Claude's reasoning is kept on every analysis so a later "why was this
    # flagged?" has an answer and the alert threshold can be tuned.
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Operator workflow state, per (article, client): the same story can be
    # handled for one mandate and still open for another. Defaults to NEU at the
    # DB level so a raw INSERT (or an older code path) can never leave it null.
    # Judged from the client's perspective; see Tonality. Kept separate from
    # importance: a 9/10 story can be a triumph or a disaster, and the two
    # questions need different answers.
    tonality: Mapped[Tonality] = mapped_column(
        SAEnum(
            Tonality,
            values_callable=lambda enum: [m.value for m in enum],
            create_constraint=True,
        ),
        nullable=False,
        default=Tonality.UNBEKANNT,
        server_default=Tonality.UNBEKANNT.value,
    )
    triage_state: Mapped[TriageState] = mapped_column(
        SAEnum(
            TriageState,
            values_callable=lambda enum: [m.value for m in enum],
            create_constraint=True,
        ),
        nullable=False,
        default=TriageState.NEU,
        server_default=TriageState.NEU.value,
    )
    analyzed_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )

    article: Mapped["Article"] = relationship(back_populates="analyses")
    client: Mapped["Client"] = relationship(back_populates="analyses")

    __table_args__ = (
        # One story about one client is analysed exactly once.
        UniqueConstraint("article_id", "client_id", name="uq_analyses_article_client"),
        CheckConstraint(
            f"relevance_score >= {SCORE_MIN} AND relevance_score <= {SCORE_MAX}",
            name="ck_analyses_relevance_range",
        ),
        CheckConstraint(
            f"importance_score >= {SCORE_MIN} AND importance_score <= {SCORE_MAX}",
            name="ck_analyses_importance_range",
        ),
    )


class Run(Base):
    """One daily sweep."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    status: Mapped[RunStatus] = mapped_column(
        SAEnum(
            RunStatus,
            values_callable=lambda enum: [m.value for m in enum],
            create_constraint=True,
        ),
        nullable=False,
        default=RunStatus.OK,
    )
    articles_found: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # Per-feed / per-batch error messages collected during the sweep.
    errors: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON),
        default=list,
        nullable=False,
        server_default=_EMPTY_JSON_ARRAY,
    )


class Setting(Base):
    """Simple key/value app settings (alert threshold, active feeds, ...)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)


class Advisory(Base):
    """One generated set of suggested PR actions for a client.

    Kept as a historical record rather than a single overwritten row: what was
    advised on the day a crisis broke is itself worth being able to look back at,
    and the newest row is simply the current view. ``suggestions`` holds the
    validated payload as JSON — the shape belongs to ``schemas.AdvisoryBrief``,
    so it can evolve without a migration per field.
    """

    __tablename__ = "advisories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    generated_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )
    # The window of coverage the advice was based on, so a stale brief is
    # recognisable as stale rather than silently out of date.
    covered_days: Mapped[int] = mapped_column(Integer, nullable=False)
    article_count: Mapped[int] = mapped_column(Integer, nullable=False)
    situation: Mapped[str] = mapped_column(Text, nullable=False)
    suggestions: Mapped[list[dict]] = mapped_column(
        MutableList.as_mutable(JSON),
        default=list,
        nullable=False,
        server_default=_EMPTY_JSON_ARRAY,
    )


__all__ = [
    "Base",
    "Category",
    "RunStatus",
    "Tonality",
    "TriageState",
    "Client",
    "client_competitors",
    "Article",
    "Analysis",
    "Advisory",
    "Run",
    "Setting",
    "DEFAULT_COUNTRY",
    "SCORE_MIN",
    "SCORE_MAX",
]
