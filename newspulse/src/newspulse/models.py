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


class AssetKind(StrEnum):
    """The formats an impulse can become, beside the letter.

    Named here so the six are spelled once rather than quoted in five modules and
    three templates. What this is *not* is the authority on which formats exist:
    that is the registry in :mod:`newspulse.assets`, and ``assets.kind`` is a
    plain string column for exactly that reason. A CHECK constraint here would
    make a seventh format a schema migration, when the whole point of holding a
    format as data is that a seventh is a definition and a prompt file.

    The letter is deliberately not in here. It has a recipient and its own
    ledger, and it stays in :class:`Outreach`.
    """

    PRESSEMITTEILUNG = "pressemitteilung"
    STATEMENT = "statement"
    QA = "qa"
    TALKING_POINTS = "talking_points"
    GASTBEITRAG = "gastbeitrag"
    INTERVIEW_BRIEFING = "interview_briefing"


class CheckState(StrEnum):
    """Where a generated text stands with the two models that read it.

    Three states rather than a boolean, for the reason the outreach review
    columns exist: "nothing objected" and "nothing looked" must never render
    alike. The second is the one that ships a fabricated quote.
    """

    UNGEPRUEFT = "ungeprueft"
    EINWAND = "einwand"
    GEPRUEFT = "geprueft"


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
    # The company's own site, used to fetch its logo once at creation and as the
    # obvious place to click through from a card.
    website: Mapped[str | None] = mapped_column(String(512), nullable=True)
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
    # Categories this mandate never wants in its daily feed. Per client, because
    # "finanzen" is three near-identical ticker items a day for a listed retailer
    # and the entire mandate for a bank. Hiding, not discarding: the articles stay
    # in the archive, in the counts and in the export — a muted category is a
    # reading preference, and a number that silently changed with one would be a
    # different and much worse problem.
    muted_categories: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON),
        default=list,
        nullable=False,
        server_default=_EMPTY_JSON_ARRAY,
    )
    # Why this mandate has no current positioning, and when that was last
    # established. On the client rather than in the web process, because the
    # answer is produced by the 06:10 sweep and read by a person at nine — an
    # in-memory note written only by the button meant the page stayed silent
    # about every unattended attempt, which is how "es funktioniert immer noch
    # nicht" came back six times over work that was running correctly.
    impulse_note: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    impulse_checked_at: Mapped[dt.datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
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
    # What this client wants to say, in what tone, and what it never says. Keywords
    # describe what is written *about* them; this describes what they stand for,
    # which every generated text otherwise has to guess. Prepended to the angle,
    # advisory and assistant prompts, so it is deliberately short — see
    # web.routes.guide.GUIDE_MAX_CHARS.
    comms_guide: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
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

    # The mandates this company is a yardstick for — the same table read the
    # other way. Not a backref on ``competitors``: both directions are stored
    # explicitly (see the table's own comment), so "X is measured against Y" must
    # not silently imply the reverse. This is only for saying, on a competitor's
    # row, whose comparison set it belongs to — a portfolio where every company
    # looks alike is how a finance platform ended up beside fashion brands.
    benchmark_for: Mapped[list["Client"]] = relationship(
        "Client",
        secondary=client_competitors,
        primaryjoin=lambda: Client.id == client_competitors.c.competitor_id,
        secondaryjoin=lambda: Client.id == client_competitors.c.client_id,
        viewonly=True,
        lazy="selectin",
    )

    analyses: Mapped[list["Analysis"]] = relationship(
        back_populates="client", cascade="all, delete-orphan"
    )


#: The floor for "this analysis concerns its client". A relevance of 0 is the
#: analyzer's way of saying a matched pair does not actually concern the client.
MIN_RELEVANCE = 1


def visible_coverage():
    """The one condition every view of a client's coverage must apply.

    There were nine copies of ``relevance_score >= 1`` across as many modules, and
    adding a second reason to hide a row — a human dismissing it — would have meant
    finding all of them and never missing one. One predicate cannot drift, and a
    dismissed article cannot survive in the corner nobody remembered.
    """
    from sqlalchemy import and_

    return and_(
        Analysis.relevance_score >= MIN_RELEVANCE,
        Analysis.dismissed_at.is_(None),
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
    # Set when a human said "this is not about my client". The matcher favours
    # recall on purpose, so a company named after a fictional planet — or one with
    # a namesake in another industry — collects articles that simply are not about
    # it, and until now there was no way to take them out.
    #
    # Dated rather than flagged, so the decision carries when it was made. And the
    # row *stays*: deleting the analysis would let the next sweep re-match the pair
    # and analyse it again, and the article would be back by morning. This row is
    # exactly what stops that.
    dismissed_at: Mapped[dt.datetime | None] = mapped_column(
        UTCDateTime(), nullable=True, index=True
    )
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


class GuideSource(Base):
    """One document a client's communications guide was distilled from.

    The extracted **text** is stored, not the file. The text is what the
    distillation reads and what a later re-run needs; the layout is not, and
    keeping binaries out of a SQLite file that gets copied on every deploy is
    worth more than being able to hand the original back.
    """

    __tablename__ = "guide_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    # Kept so the source list can say how much was read without loading the text.
    characters: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    uploaded_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )


class Contact(Base):
    """One journalist the consultant knows, with whatever they know about them.

    The one place in this tool where contact details live, and they get here
    exactly one way: a person types them in. Nothing here is derived, scraped or
    guessed — feeds carry a byline sometimes and an address never, and a
    plausible "vorname.nachname@medium.de" is worse than an empty field because
    it gets used and reaches the wrong person.

    Keyed on (name, outlet) rather than name alone: a journalist who moves is a
    new contact at the new masthead, and the pitch list looks people up by the
    outlet that published them.
    """

    __tablename__ = "contacts"
    __table_args__ = (
        UniqueConstraint("name", "outlet", name="uq_contacts_name_outlet"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    #: The masthead they wrote under. Empty is allowed — a freelancer known by
    #: name is still worth keeping.
    outlet: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    email: Mapped[str] = mapped_column(String(320), nullable=False, default="")
    phone: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    #: What they write about, in the consultant's own words.
    beat: Mapped[str] = mapped_column(String(400), nullable=False, default="")
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    @property
    def has_details(self) -> bool:
        """Whether anything was actually filled in beyond the name."""
        return bool(self.email or self.phone or self.beat or self.notes)


class TopicHit(Base):
    """One market article the topic radar surfaced for one client.

    The counterpart to :class:`Analysis`, and deliberately not the same thing.
    An analysis says "this article is about this client"; a topic hit says "this
    article is about what this client does, and never mentions them". Filing the
    second as the first would put a story about the market into a mandate's own
    coverage count, which is the number the whole tool is judged on.

    It exists because the pairing is otherwise lost. The radar carries it in
    memory during a run — the client's themes are what found the article, nothing
    in the article says so — and without a row here the market material is in the
    database but attached to nobody: unbrowsable, and unusable for ranking which
    outlets cover a client's subject.
    """

    __tablename__ = "topic_hits"
    __table_args__ = (
        # One row per (article, client): a re-run that re-surfaces the same story
        # must not stack duplicates, the same posture as analyses.
        UniqueConstraint("article_id", "client_id", name="uq_topic_hit_article_client"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # When the radar surfaced it, not when it was published: the market view is a
    # log of what the tool saw, and a story can surface days after it ran.
    found_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )


class Angle(Base):
    """One drafted positioning message for a client, off a market development.

    Distinct from :class:`Advisory` on purpose. An advisory is internal — what the
    consultant should *do*, in his own language, generated when he asks. This is
    outward-facing text he could forward to the mandate, produced unasked by the
    daily sweep, and it comes from coverage that does not mention the client at
    all (the topic radar). Same client, opposite direction; folding them into one
    table would mean one of the two lying about what it is.

    The prose fields are columns rather than a JSON blob because they are read,
    searched and shown individually; ``statements`` and ``article_ids`` are lists
    and stay JSON. ``article_ids`` points into ``articles`` rather than duplicating
    headlines, so a draft always cites the story as stored.
    """

    __tablename__ = "angles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Indexed: the Today view asks for one local day's drafts on every render.
    generated_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow, index=True
    )
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[str] = mapped_column(Text, nullable=False)
    credibility: Mapped[str] = mapped_column(Text, nullable=False, default="")
    thesis: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # The overclaim the draft deliberately avoids. Stored because it is what makes
    # the draft checkable: it says out loud which stronger claim was rejected.
    overclaim: Mapped[str] = mapped_column(Text, nullable=False, default="")
    statements: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON),
        default=list,
        nullable=False,
        server_default=_EMPTY_JSON_ARRAY,
    )
    article_ids: Mapped[list[int]] = mapped_column(
        MutableList.as_mutable(JSON),
        default=list,
        nullable=False,
        server_default=_EMPTY_JSON_ARRAY,
    )


class ClientFact(Base):
    """One stated fact about a mandate, with where it came from.

    The profile could have been fifteen columns on ``clients``. It is a table
    instead, for one reason: a fact a machine found on the internet and a fact the
    consultant typed himself must never look alike. Every row carries its source
    and who put it there, so the page can show "CEO: Alexandre Prot" next to the
    page it was read from, and the consultant can overrule it with one that has no
    source at all — his own knowledge, which outranks both.

    Keyed on (client, key): a mandate has one CEO field, and filling it again
    replaces the answer rather than growing a list of guesses.
    """

    __tablename__ = "client_facts"
    __table_args__ = (
        UniqueConstraint("client_id", "key", name="uq_client_facts_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: A key from :data:`newspulse.profile.FIELDS`. Free-form in the schema so a
    #: new field is a code change rather than a migration.
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: Where it was read. Empty when a person typed it, which is the strongest
    #: provenance there is and needs no link.
    source_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: "mensch" or the model that proposed it. Rendered, because a profile that
    #: hides which half a machine wrote is a profile nobody can audit.
    filled_by: Mapped[str] = mapped_column(String(80), nullable=False, default="mensch")
    updated_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )


class Outreach(Base):
    """One personalised message: an impulse, written at a named recipient.

    The page used to carry two panels — a positioning draft from the market and a
    "recommendation" from the mandate's own press — and the difference between
    them was never legible: "das ist wirklich nicht ganz klar wo der unterschied
    liegt". There is only one thing a consultant does with either: send a text to
    a journalist. So the recommendation stopped being a second panel and became
    this: the same impulse, aimed.

    Kept in its own table rather than as columns on ``angles`` because one impulse
    has many of these — one per recipient — and each is a separate artefact with
    its own moment of creation.

    ``journalist`` may be empty: a feed carries a byline about one time in ten,
    and an outlet with no name attached is still a valid address for a pitch.
    """

    __tablename__ = "outreach"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    angle_id: Mapped[int] = mapped_column(
        ForeignKey("angles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    generated_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow, index=True
    )
    journalist: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    outlet: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    #: The subject line as it would go out — this one *is* for the recipient,
    #: unlike an angle's subject, which is a label for the consultant.
    subject: Mapped[str] = mapped_column(Text, nullable=False, default="")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    #: Why this recipient, in one line: what they wrote that this answers. Kept
    #: apart from the message so it can never be pasted into an inbox with it.
    hook: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: A second model's verdict on the letter, one concern per line. Empty means
    #: either "no concerns" or "never checked", which the two columns below
    #: distinguish — a blank check and a clean check must not look alike.
    review: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: Which model checked it, so a stale verdict from a since-changed provider is
    #: visible rather than silently authoritative. Empty means unchecked.
    reviewed_by: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    #: The checker's own send/hold flag. True unless it objected.
    review_ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Asset(Base):
    """One generated text in one format: a release, a statement, a Q&A, a briefing.

    A new table rather than six more columns on :class:`Outreach`, and the
    reason is what those columns *are*. A letter's ``journalist`` and ``outlet``
    are its recipient; five of the six formats here have no recipient at all. On
    one shared table most rows would carry mostly empty columns and every query
    would have to guess which shape it was reading. The letter keeps its table
    and its ledger, and the two are read together where both belong.

    ``angle_id`` is not decoration: it is what makes a text traceable back to
    the position it argues and the coverage under that position. A stored text
    whose impulse is unknown cannot be checked by anyone.

    ``kind`` names the format definition this row was written against. It is a
    string rather than a DB enum on purpose: the registry in
    :mod:`newspulse.assets` decides which formats exist, and a CHECK constraint
    here would turn every new format into a schema migration. A kind the registry
    does not know fails loudly at the lookup, which is where it should.
    """

    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    angle_id: Mapped[int] = mapped_column(
        ForeignKey("angles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    # Indexed for the same reason as the letter's: a day's texts are asked for
    # on every render of the Today column.
    generated_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow, index=True
    )
    #: Headline, subject line or briefing title, depending on the format. Empty
    #: for the formats that have no title of their own.
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False)
    #: Who the text is attributed to, for the formats that quote a person. Never
    #: derived and never invented: it is copied from the mandate's profile, and a
    #: format that needs it will not be written without it.
    speaker: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    #: When a human last changed the text. ``None`` means the model's words are
    #: still exactly as they came back, which is a different artefact from one a
    #: person has been through.
    edited_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    #: The second model's verdict, one concern per line. The three columns mirror
    #: :class:`Outreach` exactly, so a letter and a release mean the same thing by
    #: "gegengelesen".
    review: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reviewed_by: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    review_ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: The same three for the check against the mandate's own guide. Kept apart
    #: from the crosscheck because a No-Go is not a judgement about the world:
    #: the client wrote it down, and it must never be averaged into a style note.
    guide_review: Mapped[str] = mapped_column(Text, nullable=False, default="")
    guide_reviewed_by: Mapped[str] = mapped_column(
        String(80), nullable=False, default=""
    )
    guide_review_ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: The human release. Grade F, without exception: nothing leaves this tool
    #: because a model was content with it.
    released_at: Mapped[dt.datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )
    released_by: Mapped[str] = mapped_column(String(80), nullable=False, default="")

    @property
    def released(self) -> bool:
        return self.released_at is not None

    @property
    def check_state(self) -> CheckState:
        """Unchecked, objected to, or clean. Never clean by omission.

        Both checkers silent means nothing has read this text, and that is the
        one state the page must not draw as a clean bill of health.
        """
        if not (self.reviewed_by or self.guide_reviewed_by):
            return CheckState.UNGEPRUEFT
        if not self.review_ok or not self.guide_review_ok:
            return CheckState.EINWAND
        return CheckState.GEPRUEFT


__all__ = [
    "Base",
    "Category",
    "CheckState",
    "AssetKind",
    "RunStatus",
    "Tonality",
    "TriageState",
    "Client",
    "client_competitors",
    "Article",
    "Analysis",
    "Advisory",
    "Angle",
    "ClientFact",
    "Outreach",
    "Asset",
    "TopicHit",
    "GuideSource",
    "Run",
    "Setting",
    "DEFAULT_COUNTRY",
    "MIN_RELEVANCE",
    "visible_coverage",
    "SCORE_MIN",
    "SCORE_MAX",
]
