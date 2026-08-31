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

# The same ``text`` under a second name. ``VisibilityQuestion`` has a column
# called ``text``, which shadows the import inside that class body, and
# ``server_default=text("1")`` there would call the column object instead.
from sqlalchemy import text as sql_text
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


class SignalKind(StrEnum):
    """The three market classes a news feed cannot carry.

    Each breaks the shape of a news item in a way that matters to a consultant. A
    ``studie`` has already been published but stays citable for months, which is
    the opposite of a story whose value decays in days. A ``regulierung`` is dated
    in the *future*, and its entire value is the lead time — a feed that reports
    what already happened delivers it on the day it is too late to say anything.
    A ``veranstaltung`` is a date and a stage, and the only class that carries a
    deadline, because a call for speakers closes.

    A closed set, so a fourth class is a deliberate migration rather than a string
    somebody spelled two ways.
    """

    STUDIE = "studie"
    REGULIERUNG = "regulierung"
    VERANSTALTUNG = "veranstaltung"


class SignalOrigin(StrEnum):
    """Which half of the market radar produced a signal (DEC-1 B).

    The curated list is the half that is what it says it is: a statistics office
    publishes studies and nothing else. The per-mandate search is the half that
    covers a field nobody curated for, and it will return things that are not
    really studies. A reader has to be able to judge a search-found row as one, so
    the provenance is stored rather than guessed from the publisher's name.
    """

    KURATIERT = "kuratiert"
    SUCHE = "suche"


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


class OutreachState(StrEnum):
    """Where one letter stands between being written and having produced something.

    The list is short on purpose, and one obvious member is missing: there is no
    "ohne Reaktion". Silence is not something anybody enters — it is ``RAUS`` plus
    time, derived at read (:func:`newspulse.outreach.is_silent`), so the ledger
    never claims a fact nobody recorded. Storing it would mean a nightly job that
    rewrites rows to assert an absence, and a row that says "no answer" on a day
    the answer arrived.

    ``ENTWURF`` is the only state a machine may set; the other four are a person's
    reading of what came back. That is why ``ABSAGE`` and ``VEROEFFENTLICHT`` are
    separate from ``ANTWORT``: "danke, nichts für uns" and "schicken Sie mehr" are
    the same event to a matcher and opposite events to a consultant.
    """

    ENTWURF = "entwurf"
    RAUS = "raus"
    ANTWORT = "antwort"
    ABSAGE = "absage"
    VEROEFFENTLICHT = "veroeffentlicht"


#: How long a released letter may go unanswered before the card calls it still.
#: Two weeks: a journalist who has not replied inside one is busy, and one who has
#: not replied inside three was never going to. It is a display threshold, not a
#: stored state — see :class:`OutreachState`.
SILENT_AFTER_DAYS = 14


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
    # The market classes this mandate never wants — the same shape and the same
    # reasoning as ``muted_categories`` above, one level out: a regulatory
    # calendar is the whole job for a bank and pure noise for a fashion label.
    #
    # It differs from the category mute in one way, and deliberately. A muted
    # category still arrives and is merely hidden, because the archive and the
    # counts must not move with a reading preference. A muted class is not
    # fetched at all on the next sweep, because a market signal is not coverage:
    # nothing counts it, no report is judged on it, and there is nobody to be
    # honest to about a study the mandate has said it does not want. Fetching it
    # anyway would spend a dozen requests a morning on a page nobody looks at.
    muted_signal_kinds: Mapped[list[str]] = mapped_column(
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
    # When the deep-dive profile was last re-read from the web, whatever that read
    # produced. Same reasoning as ``impulse_checked_at`` above: the answer is
    # produced by an unattended sweep and read by a person hours later, so it has
    # to be on the client rather than in the process that produced it. NULL means
    # never checked, which the page says out loud — a profile that has aged for a
    # year and one that was checked this morning must not look alike.
    profile_checked_at: Mapped[dt.datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )
    #: Why the last profile check produced nothing, empty when it produced
    #: something or found nothing to change. The stamp above is set on every
    #: attempt including a failed one, which is right — an attempt happened — and
    #: on its own it makes a mandate whose research broke read as "geprüft: heute"
    #: while quieting its age trigger for sixty days. Exactly the hole
    #: ``impulse_note`` was added to close, so it is closed the same way.
    profile_note: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    #: Whether this mandate's industry term is one the German press writes often
    #: enough to search with, and when that was last established. Same reasoning
    #: as the two stamps above: the answer costs a live search per term, it is
    #: produced by the 06:10 sweep, and it is read by a person at nine — asking
    #: it while a page renders would put a twenty-second feed timeout inside a
    #: GET, on exactly the mandates the answer exists for.
    #:
    #: NULL is the third answer and it is load-bearing: the question has not been
    #: put, or the last attempt could not reach the search at all. The market page
    #: says nothing in that case, because an unreachable search is not evidence
    #: about a word, and sending an operator off to fix a term that works is worse
    #: than the silence it replaces.
    field_usable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    field_checked_at: Mapped[dt.datetime | None] = mapped_column(
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

    def mutes_signal(self, kind: SignalKind | str) -> bool:
        """Whether this mandate has switched off one market class.

        On the model rather than in either caller because both the sweep that
        must not fetch it and the page that must not show it have to agree on
        the answer, and two readings of the same list is how a class ends up
        fetched every morning for a page that hides it.
        """
        return str(getattr(kind, "value", kind)) in (self.muted_signal_kinds or [])


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


class BrainOverride(Base):
    """One recorded change to what the house believes, block by block.

    The repository ships the blocks (``newspulse/blocks/*.txt``) and they stay
    the default underneath, so a fresh install thinks correctly on day one and
    git keeps the lineage. This table is what the agency writes on top of them,
    because what good PR looks like is the agency's judgement and not the
    developer's, and a consultant should not need a deployment to change a
    sentence about tone.

    Append-only: a row is an *event*, not the current state. The override in
    force for a block is its newest row, and a revert is a row of its own rather
    than the deletion of one — "we went back to the shipped wording in September"
    is a decision somebody made, and a letter written the week before was written
    under a different standard. Deleting the row would make the revert look like
    it never happened, which is exactly the history this table exists to keep.

    ``text`` is NULL on precisely those revert rows. NULL and ``""`` are
    different answers: the empty string is refused at write time (see
    :func:`newspulse.brain.edit`), because a prompt composing an empty standard
    drops it in silence rather than complaining.
    """

    __tablename__ = "brain_overrides"
    __table_args__ = (
        # One version per recorded change, enforced rather than assumed: BRN-03
        # stamps generated texts with a version and reads the standards back out
        # of this table, so two rows sharing a number would make that lookup
        # ambiguous in the one conversation where it matters.
        UniqueConstraint("version", name="uq_brain_overrides_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: The block's stable key — the same one a prompt's ``{{brain:…}}`` names.
    #: Deliberately not a foreign key to anything: the blocks are files, and an
    #: override whose file was renamed away has to stay findable (the settings
    #: panel shows it as orphaned) rather than vanish with it.
    key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: The overriding text, or NULL for "back to the shipped default".
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    edited_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )
    #: Who changed it. One shared Basic-auth credential is the only identity this
    #: tool has, so this is that user name or ``"mensch"`` — never a name nobody
    #: supplied.
    edited_by: Mapped[str] = mapped_column(String(80), nullable=False, default="mensch")
    #: The portfolio-wide brain version this change produced, counting every
    #: recorded change across every block. One number for the whole house, so a
    #: text can say which standards it was written under with a single integer.
    version: Mapped[int] = mapped_column(Integer, nullable=False)


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
    #: The standards this brief was written under, on the same terms as
    #: :attr:`Angle.brain_version`. Stamped even though the advisor has no page
    #: of its own any more: it composes the same blocks and stores what a model
    #: wrote, and a stamp that is only on the convenient generators is a stamp
    #: nobody can trust the absence of.
    brain_version: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
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


class MarketSignal(Base):
    """One study, one regulatory date or one event, for one client.

    Deliberately its own table rather than a flag on :class:`Article`. An article
    is what a feed syndicated *about a company*: it obeys the no-body-text rule
    for Leistungsschutzrecht reasons, it has already happened, and every query in
    the tool that touches coverage assumes exactly that shape. A consultation that
    closes in five weeks is none of those three things, and filing it in
    ``articles`` would make each of those queries wrong in a way nobody would
    notice until a client report counted a consultation as press.

    Scoped to a client for the same reason :class:`TopicHit` is: what makes a
    signal belong to a mandate is which mandate it was fetched for, and nothing in
    the item's own text says so.

    Four dates rather than one, because "when did this happen" is the wrong
    question for two of the three classes:

    * ``found_at`` — when the sweep saw it. Never the date a reader is shown; a
      log of what the tool did is not a calendar.
    * ``published_at`` — when the source put it out. The actionable date for a
      study, and usually the only one a study has.
    * ``effective_at`` — when it *lands* or opens: a law taking effect, a
      consultation opening, an event's own date. Routinely in the future, which is
      the entire point of the regulatory class, so nothing anywhere in the
      pipeline may treat that as an error or clamp it to now.
    * ``deadline_at`` — when the door closes: a consultation's cut-off, a call for
      speakers. Only the classes that have one carry it, and it is separate from
      ``effective_at`` because "you may still speak" and "it now applies to you"
      are opposite instructions.

    ``title_hash`` is the same normalized-title hash ``articles`` carries, stored
    for the same reason: an official source that re-issues a page under a new URL
    would otherwise arrive as a second signal every sweep. It is nullable, because
    :func:`newspulse.matching.dedup_title_hash` refuses a headline too thin to
    trust — and a NULL does not collide in a UNIQUE index, so such a row falls
    back to URL identity exactly as the article dedup does.
    """

    __tablename__ = "market_signals"
    __table_args__ = (
        # "url unique per client", not globally: the same consultation is a real
        # signal for every mandate in the field, and each of them has to be able
        # to mute, read and report it on its own.
        UniqueConstraint("client_id", "url", name="uq_market_signal_client_url"),
        # And the same item under a changed URL. Per class as well as per client,
        # so a conference and the study it presents can share a headline without
        # one of them silently disappearing.
        UniqueConstraint(
            "client_id", "kind", "title_hash", name="uq_market_signal_client_kind_title"
        ),
        # The market page ranks by what is next (DEC-2 C), so this is the column
        # every read of it orders on.
        Index("ix_market_signals_effective_at", "effective_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[SignalKind] = mapped_column(
        SAEnum(
            SignalKind,
            values_callable=lambda enum: [m.value for m in enum],
            create_constraint=True,
        ),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    # Who published it — the institute, the authority, the organiser. Not
    # nullable: a study whose publisher is unknown cannot be cited, and the empty
    # string says that out loud rather than hiding behind a NULL.
    publisher: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    found_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )
    published_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    effective_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    deadline_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # The source's own summary line, exactly as syndicated. The same no-scrape
    # rule as ``articles.summary_text``: this is the only body-ish text stored.
    summary: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    origin: Mapped[SignalOrigin] = mapped_column(
        SAEnum(
            SignalOrigin,
            values_callable=lambda enum: [m.value for m in enum],
            create_constraint=True,
        ),
        nullable=False,
        default=SignalOrigin.KURATIERT,
        server_default=SignalOrigin.KURATIERT.value,
    )
    title_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


class HookSource(StrEnum):
    """The three evidenced sources a plan hook may come from — and the only three.

    DEC-4 option A, as a closed set: a dated market signal, a theme the trade
    press measurably writes about, or a previous-year month the archive shows
    carried coverage. There is deliberately no fourth member for "the model knows
    a recurring date" — a plan with one invented date is a plan nobody checks a
    second time, so a hook class that cannot resolve to a stored row does not
    exist here.
    """

    MARKTSIGNAL = "marktsignal"
    THEMA = "thema"
    VORJAHR = "vorjahr"


class HookState(StrEnum):
    """Where one hook stands with the person who reads the plan.

    ``VORGESCHLAGEN`` is the only state a machine may set, and the only state a
    recompute may replace. The other two are a human decision, and a decision
    survives every recompute — a "verworfen" that came back the next morning
    would train the reader to stop deciding at all. A hook moved to another
    month keeps whatever state it has; the move is recorded on
    :attr:`PlanHook.moved_at`, and either mark counts as touched.
    """

    VORGESCHLAGEN = "vorgeschlagen"
    ANGENOMMEN = "angenommen"
    VERWORFEN = "verworfen"


class PlanHook(Base):
    """One dated entry in a mandate's editorial plan, resolving to a stored row.

    ``month`` is a ``"YYYY-MM"`` string and ``day`` is nullable, and that split is
    the date rule of the whole feature: a source that carries a full date (a
    market signal's effective date or deadline) yields a day, a source that only
    carries a month (the previous year's archive, a theme's current resonance)
    yields none — and no code path anywhere guesses the missing day. Lexicographic
    order on the month string is chronological order, which is what every read of
    the plan sorts by.

    ``source_kind``/``source_id`` are the evidence: the id of the row in the table
    the kind names (``market_signals``, ``topic_hits``, ``analyses``). Not a real
    foreign key, because it points into one of three tables depending on the kind
    — :func:`newspulse.plan._resolves` is the guard instead, and a hook whose
    evidence does not resolve is never stored. The UNIQUE below is what makes a
    recompute unable to file a second hook off a row a person already decided on.

    ``reason`` and ``format`` are the only two fields a model writes, and neither
    of them is load-bearing: the hook exists because of its evidence, and a
    recompute whose model call failed stores the hook with empty prose rather
    than dropping a documented date.
    """

    __tablename__ = "plan_hooks"
    __table_args__ = (
        # One hook per evidence row per mandate, enforced rather than assumed: the
        # recompute skips sources that already carry a hook, and this is what
        # keeps a race (or a bug in the skip) from stacking a fresh proposal next
        # to the "verworfen" a person already recorded against the same row.
        UniqueConstraint(
            "client_id", "source_kind", "source_id", name="uq_plan_hooks_source"
        ),
        # Every read of the plan asks for one mandate's months in the window.
        Index("ix_plan_hooks_client_month", "client_id", "month"),
        # 1-based like a calendar; NULL is the honest "the source only names a
        # month". The upper bound is the widest month rather than per-month
        # arithmetic — the day is copied from a stored datetime, which cannot
        # produce February 30th, so this catches raw-INSERT garbage only.
        CheckConstraint(
            "day IS NULL OR (day >= 1 AND day <= 31)", name="ck_plan_hooks_day"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_kind: Mapped[HookSource] = mapped_column(
        SAEnum(
            HookSource,
            values_callable=lambda enum: [m.value for m in enum],
            create_constraint=True,
            name="hook_source",
        ),
        nullable=False,
    )
    source_id: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[str] = mapped_column(String(7), nullable=False)
    day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: What the hook is about, copied from the evidence row (a signal's title, a
    #: theme's term, the strongest headline of the carried month). Derived by
    #: code, never by a model.
    title: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    #: Why this date is an occasion for this mandate — the model's prose, and the
    #: one thing here a model is allowed to write. Empty when the call failed.
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    #: The suggested format, one of the keys :data:`newspulse.assets.REGISTRY`
    #: knows, or empty. A key the registry does not know is dropped at store time
    #: rather than kept: the plan page pre-selects this in the format picker, and
    #: an invented key would break exactly that click.
    format: Mapped[str] = mapped_column(
        String(40), nullable=False, default="", server_default=""
    )
    state: Mapped[HookState] = mapped_column(
        SAEnum(
            HookState,
            values_callable=lambda enum: [m.value for m in enum],
            create_constraint=True,
            name="hook_state",
        ),
        nullable=False,
        default=HookState.VORGESCHLAGEN,
        server_default=HookState.VORGESCHLAGEN.value,
    )
    #: When a person moved it to another month. A move is a touch: a moved hook
    #: survives every recompute even while its state is still "vorgeschlagen".
    moved_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    #: When a person accepted or discarded it. Empty exactly while the state is
    #: the machine's.
    decided_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )
    #: The standards the reason was written under, on the same terms as
    #: :attr:`Angle.brain_version`: captured when the prompt is composed, and
    #: NULL only for a row from before there was anything to stamp. The reason
    #: lands verbatim on a page a client reads, which is exactly the kind of
    #: text the stamp exists for.
    brain_version: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
    )

    @property
    def touched(self) -> bool:
        """Whether a person has done anything to this hook.

        The recompute's whole contract in one place: only an untouched hook may
        be replaced. Accepting, discarding and moving are the three touches, and
        the first two live in ``state`` while the third lives in ``moved_at`` —
        a hook moved to October is still a proposal, but it is a person's
        proposal now.
        """
        return self.state is not HookState.VORGESCHLAGEN or self.moved_at is not None


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
    __table_args__ = (
        # One occasion per plan hook, enforced rather than assumed.
        # ``plan_view.occasion_for`` reads for an existing one and then inserts,
        # and FastAPI runs those routes in a threadpool: a double-clicked "Text
        # schreiben" is two requests that both see nothing and both write, which
        # leaves the hook with two occasions carrying the same date and the page
        # linking at whichever one it happened to read. Partial, because
        # ``plan_hook_id`` is NULL for nearly every impulse on file — the radar
        # drafts them and there is no hook — and a plain unique index would
        # allow exactly one of those in the whole table.
        Index(
            "ux_angles_plan_hook",
            "plan_hook_id",
            unique=True,
            sqlite_where=text("plan_hook_id IS NOT NULL"),
            postgresql_where=text("plan_hook_id IS NOT NULL"),
        ),
    )

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
    #: Which standards this draft was written under: the portfolio-wide brain
    #: version (:func:`newspulse.brain.version`) as it stood when the *prompt* was
    #: composed, not when the row was saved. A consultant editing a standard while
    #: a sweep is running must not retroactively change what a finished text
    #: claims to have been written under.
    #:
    #: NULL means "unknown", which is a different answer from ``0``. Zero is a
    #: true statement — the standards have never been changed on this install —
    #: and a row written before this column existed cannot make it. So the column
    #: is nullable with no server default, and the interface says "unbekannt"
    #: rather than claiming standards that were never recorded.
    brain_version: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
    )
    #: The plan hook this occasion was opened from, when it was opened from one.
    #:
    #: An impulse normally comes from the radar and belongs to the morning it was
    #: drafted. A hook is the other way round: a person clicked "Text schreiben"
    #: on a dated entry in the editorial plan, and the texts written afterwards
    #: are that hook's texts — which is what makes the plan page able to say
    #: "Gastbeitrag am 03.09. freigegeben" beside the entry it came from.
    #: :class:`Asset` keeps hanging on the occasion rather than on the hook, so a
    #: format needs to know nothing about where its occasion came from.
    #:
    #: NULL for every impulse the radar drafted, which is nearly all of them.
    #: ``SET NULL`` rather than ``CASCADE``: a hook a recompute removed must not
    #: take a released press release with it.
    #: Indexed by ``ux_angles_plan_hook`` in ``__table_args__`` rather than here:
    #: that index is unique and partial, and a second plain one over the same
    #: column would be dead weight on every write.
    plan_hook_id: Mapped[int | None] = mapped_column(
        ForeignKey("plan_hooks.id", ondelete="SET NULL"),
        nullable=True,
        default=None,
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
    #: What this field said before a kick-off answer replaced it, and where *that*
    #: came from. DEC-2 option A: the person who knows the company outranks the
    #: page written about it, so the answer wins — but the disagreement stays
    #: legible instead of being erased, because a researched value that a client
    #: contradicts is a fact about the coverage even after it stops being a fact
    #: about the company. Empty on almost every row: it fills only where an answer
    #: and the web actually disagree.
    superseded_value: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    superseded_source_url: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    superseded_source_title: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    superseded_filled_by: Mapped[str] = mapped_column(
        String(80), nullable=False, default="", server_default=""
    )
    superseded_at: Mapped[dt.datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )

    @property
    def is_disputed(self) -> bool:
        """Whether an older value is still standing beside this one.

        Read by the profile page rather than having it compare a string against
        the empty one: "there is a superseded value" is the question being asked.
        The value is what answers it; ``superseded_at`` is provenance for the
        line beside it and never decides whether the line is shown.
        """
        return bool(self.superseded_value.strip())


#: Who a hand-typed kick-off answer is attributed to. DEC-1 option A: the
#: consultant sits in the call and transcribes. A name rather than a boolean,
#: because option B (the client answers directly) puts a different name here —
#: which is exactly when a second copy of this literal would drift, so the column
#: default and :mod:`newspulse.onboarding` both read it from here.
ANSWERED_BY_DEFAULT = "Berater"


class OnboardingAnswer(Base):
    """One answer from the kick-off questionnaire, as it was given.

    Deliberately *not* a ``client_facts`` row and deliberately not a paragraph on
    ``clients.comms_guide``. What the client said in the kick-off call and what
    the tool has adopted as policy are two different things, and collapsing them
    is how a sentence nobody approved becomes the rule every future text is
    checked against. This table is the raw answer; adopting it is a separate,
    deliberate act (ONB-02).

    Keyed on (client, key): a mandate answers each question once, and answering
    it again replaces what was there rather than growing a pile of versions.

    ``skipped`` exists because "asked and deliberately passed over" and "never
    got to it" are different states of the same foundation, and only one of them
    is a reason to go back to the client. A row with ``skipped`` set carries no
    value; a question with no row at all is simply still open.
    """

    __tablename__ = "onboarding_answers"
    __table_args__ = (
        UniqueConstraint("client_id", "key", name="uq_onboarding_answers_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: A key from :data:`newspulse.onboarding.QUESTIONS`. Free-form in the schema
    #: so rewording the question set is a code change, not a migration.
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    answered_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )
    #: Who put it there. The consultant transcribing a call today; the client
    #: itself once DEC-1 option B is built, which is why this is a name and not a
    #: boolean.
    answered_by: Mapped[str] = mapped_column(
        String(80), nullable=False, default=ANSWERED_BY_DEFAULT
    )
    skipped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


#: What :attr:`Outreach.outcome_by` holds when the mailbox sync recorded the
#: outcome rather than a person. A token and not a name, the way
#: ``ClientFact.filled_by`` and ``Outreach.released_by`` store "mensch": the only
#: distinction the ledger has to keep is whether a human or the machine said it.
#: Stored rather than inferred from state, note and timestamp — an inference is
#: re-derived on every render and breaks the day a retention rule deletes the
#: reply row it was reading, which would silently redraw a machine's line as a
#: sentence a consultant typed.
OUTCOME_BY_MAILBOX = "postfach"


class ProfileProposal(Base):
    """One "this looks different now", waiting for a yes.

    The background refresh proposes and never writes (see
    :mod:`newspulse.profile_refresh`), so its output has to live somewhere between
    the sweep that produced it and the person who decides on it. That used to be a
    dict in the web process, which was defensible while only a button wrote to it
    and is not now: the 06:10 sweep would produce a pile of findings and a restart
    — a deploy, a crash, a machine going to sleep — would silently drop them, and
    nobody would ever know what the tool had found.

    ``previous_value`` is copied at proposal time rather than read back at render
    time. It is what the proposal is *about*: "Umsatz: 84 Mio. (2025)" is not a
    decision anyone can make without the number it replaces beside it. Copying it
    also keeps the row honest if the fact changes underneath — the proposal still
    says which value it was arguing against.

    Keyed on (client, key) while it is outstanding: a mandate has one CEO field,
    and a second refresh replaces that client's open proposals rather than
    stacking a fresh guess beside last week's.

    A discarded row stays, stamped rather than deleted. It is the only record that
    the consultant already said no to this exact value, and without it the next
    refresh would read the same about page, find the same sentence and put the
    same rejected proposal back on the page — which is how a review pile becomes
    something nobody opens. The one exception is a row the profile has caught up
    with: the field already holds the value being proposed, so there is no claim
    left to refuse and stamping one would suppress that field's next real
    correction (see :func:`newspulse.profile_refresh.discard`).

    Which is why the uniqueness is *partial*, over the open rows only. A refusal
    is of a sentence and not of a field — "not this CEO" must not mean "never ask
    about the CEO again" — so a field accumulates one row per value that was
    refused, plus at most one still waiting for an answer. A whole-table UNIQUE
    would force the refresh to delete last month's "no" in order to file this
    month's different finding, and the value it said no to would be back on the
    page the next time a website repeated it.
    """

    __tablename__ = "profile_proposals"
    __table_args__ = (
        Index(
            "uq_profile_proposals_key",
            "client_id",
            "key",
            unique=True,
            sqlite_where=text("discarded_at IS NULL"),
        ),
        # AUTOINCREMENT, so an id is never handed out twice. The review page's
        # buttons carry row ids — "the rows I was looking at" — and a refresh
        # replaces a client's proposals by deleting and re-inserting them. A
        # plain SQLite rowid is reused after the delete, so yesterday's id could
        # come back attached to this morning's finding and a stale tab's
        # "übernehmen" would write a value nobody had read. Measured, not
        # theorised: the test for that promise failed on reused ids.
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: A key from :data:`newspulse.profile.FIELDS`, exactly as ``client_facts``
    #: holds it — a proposal is a candidate value for one of those rows.
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    # Every text column below carries ``server_default`` as well as ``default``:
    # migration 0022 emits one, so a schema built by ``Base.metadata.create_all``
    # (what the tests use) without it is a *different* schema, and an INSERT that
    # omits a column would then pass in production and fail in a test.
    value: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    #: Where it was read. A proposal without one is a machine asserting something
    #: it cannot back up, and the review page does not show it at all.
    source_url: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    source_title: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    #: What the profile said when this was proposed. Empty when the field was
    #: blank, which is the "found something new" case rather than a contradiction.
    #: Re-read from the profile when the row is refused, because a refusal is
    #: always said *against* something: "not Bob, the CEO is Anna". That is what
    #: lets the refusal expire when Anna turns out to be wrong and is cleared —
    #: see :func:`newspulse.profile_refresh._refused`.
    previous_value: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    proposed_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )
    #: The model that read the web for it. Never "mensch": a human does not
    #: propose to himself, he types the value in.
    proposed_by: Mapped[str] = mapped_column(
        String(80), nullable=False, default="", server_default=""
    )
    #: When the consultant said no, and the whole reason a discarded row is kept
    #: instead of deleted: the refresh reads it before proposing, so a value that
    #: was refused once is not offered again the next morning. An accepted row is
    #: deleted rather than stamped — the fact it became is its own memory, and a
    #: "no" recorded against a value the profile now holds would suppress a real
    #: correction later.
    discarded_at: Mapped[dt.datetime | None] = mapped_column(
        UTCDateTime(), nullable=True, default=None
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

    From the release ledger on, a row is also a record of a human act. The text of
    a released letter is frozen — a redraft for the same recipient becomes a new
    row rather than overwriting the one that went out — because the point of the
    ledger is to say what was actually sent, and an upsert would destroy exactly
    that.
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
    #: The guide check's verdict: one entry per breach, each a
    #: ``{"draft": …, "guide": …}`` pair — the sentence from the letter and the
    #: line of the client's guide it collides with. JSON rather than the
    #: newline-joined text ``review`` uses, because a breach is a *pair* of
    #: quotes and a flat line would lose which half is which; the shape belongs
    #: to ``schemas.GuideBreach``, the same arrangement ``Advisory.suggestions``
    #: has with ``schemas.ActionSuggestion``.
    guide_review: Mapped[list[dict]] = mapped_column(
        MutableList.as_mutable(JSON),
        default=list,
        nullable=False,
        server_default=_EMPTY_JSON_ARRAY,
    )
    #: Which model read the letter against the guide. Empty is the not-checked
    #: state, and it is the only field that tells it apart from a clean check —
    #: ``guide_ok`` is True in both. A client with no stored guide, an unreachable
    #: provider and an unusable reply all land here, and none of them may reach
    #: the page looking like an approval.
    guide_reviewed_by: Mapped[str] = mapped_column(
        String(80), nullable=False, default=""
    )
    #: The guide check's own flag. True unless it named a breach.
    guide_ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    @property
    def guide_breaches(self) -> list[dict[str, str]]:
        """The stored breaches that can be shown as the pair they are.

        A breach is worth showing only when both halves are there: the sentence
        from the draft *and* the line of the guide it collides with. With one of
        them missing the page would print „…“ verstößt gegen „“ under a red
        heading — an accusation with nothing under it, which is the one thing
        this block exists to let the reader settle by looking.

        :class:`newspulse.schemas.GuideBreach` makes a half pair unreachable
        through :func:`newspulse.outreach.store`, so this filters against a
        hand-edited row, a restore from a partial dump, and the next writer — the
        same defensiveness the empty-list case already has on the page. The raw
        column stays the signal for *whether* the objecting branch is entered;
        this only decides what can be printed inside it.
        """
        pairs: list[dict[str, str]] = []
        for breach in self.guide_review or []:
            if not isinstance(breach, dict):
                continue
            draft = str(breach.get("draft") or "").strip()
            line = str(breach.get("guide") or "").strip()
            if draft and line:
                pairs.append({"draft": draft, "guide": line})
        return pairs
    #: The standards this letter was written under, on the same terms as
    #: :attr:`Angle.brain_version`: captured with the prompt, NULL for a letter
    #: from before there was anything to stamp. Its own column rather than a read
    #: through ``angle_id`` — a letter is written days after the impulse it comes
    #: from, and the house may have changed its mind in between.
    brain_version: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
    )
    #: The standards the *cross-check* was composed under, which is not always the
    #: letter's. :func:`newspulse.outreach.crosscheck` builds its own brain prompt
    #: seconds after the letter, and an edit landing between the two model calls
    #: would otherwise file the checker's text under a version it never read.
    #: NULL for an unchecked letter and for every row from before the column.
    review_brain_version: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
    )

    # --- The ledger: the human act, and what came back --------------------------
    #: The contact book entry this went to, resolved once at release rather than
    #: matched again on every read. Nullable and ``SET NULL`` on delete: the
    #: recipient is also written into ``journalist``/``outlet`` on this row, so a
    #: deleted contact costs the link but never the record of who was written to.
    contact_id: Mapped[int | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    state: Mapped[OutreachState] = mapped_column(
        SAEnum(
            OutreachState,
            values_callable=lambda enum: [m.value for m in enum],
            create_constraint=True,
        ),
        nullable=False,
        default=OutreachState.ENTWURF,
        server_default=OutreachState.ENTWURF.value,
    )
    #: When a person released it. Null while it is a draft, and the one field that
    #: answers "did this leave the house": the state can be moved on by an
    #: outcome, this cannot go backwards.
    released_at: Mapped[dt.datetime | None] = mapped_column(
        UTCDateTime(), nullable=True, index=True
    )
    #: Who released it. No user accounts exist in this tool, so it defaults to
    #: "mensch" the way :attr:`ClientFact.filled_by` does — the point of the field
    #: is that a human, rather than the machine, is the accountable party.
    released_by: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    #: When the outcome was recorded, which is not when it happened: a reply read
    #: on Monday may have arrived on Saturday, and the ledger says what it knows.
    outcome_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    #: What came back, in the consultant's own words. Stored as typed: this is the
    #: one text on the row a human wrote, so the house rules that police generated
    #: prose have no business touching it.
    outcome_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: Who recorded the outcome — "mensch" for a line somebody typed,
    #: :data:`OUTCOME_BY_MAILBOX` for the one the daily sync wrote off a reply.
    #: Empty exactly while there is no outcome. The counterpart to
    #: :attr:`released_by`, and for the same reason: "wer hat das gesagt" is the
    #: first question anybody asks of a ledger line, and both pages that draw an
    #: outcome have to answer it without guessing.
    outcome_by: Mapped[str] = mapped_column(String(80), nullable=False, default="")

    # --- The thread in Gmail --------------------------------------------------
    #
    # DEC-4 locked option C: RauteOS puts the letter into Gmail and sends it. The
    # saved copy-paste is not the point — the *thread* is. Because this tool put
    # the outgoing message into it, a reply arriving days later belongs to this
    # letter as a fact rather than as a guess from a subject line, which is what
    # OUT-05's matching stands on and what lets DEC-6 option A ask Gmail for
    # nothing but the threads RauteOS started.
    #
    # All three ids are Google's, echoed back from the API response and never
    # constructed here: a locally built id would point at a thread that does not
    # exist and would be indistinguishable from one that does.
    #: The draft Gmail created before it was sent. Kept after the send, because it
    #: is the idempotency key on the way there: a second push updates *this* draft
    #: rather than composing a second one at the same journalist.
    gmail_draft_id: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    #: The conversation the letter opened. The one column OUT-05 reads.
    gmail_thread_id: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    #: The sent message itself. Set only once the message actually left, so it —
    #: not the draft id — is what "this letter went out through Gmail" means.
    gmail_message_id: Mapped[str] = mapped_column(
        String(120), nullable=False, default=""
    )

    @property
    def sent_through_gmail(self) -> bool:
        """Whether RauteOS itself put this letter into the recipient's inbox.

        Keyed on the message id rather than on the thread id: a draft that was
        composed but never sent already has a thread, and a card that read that
        as "sent" would claim an act nobody performed.
        """
        return bool(self.gmail_message_id)

    @property
    def out_through_gmail(self) -> bool:
        """Sent by RauteOS itself, and nothing back yet — the mock's red badge.

        The state is part of the question, so the red "gesendet" colouring can
        never overpaint the green an answer or a publication earns. Computed
        here rather than in the template because that is where
        :class:`OutreachState` is in scope: a card comparing against the bare
        string ``"raus"`` would quietly stop colouring anything the day the enum
        value is renamed.
        """
        return self.sent_through_gmail and self.state == OutreachState.RAUS

    @property
    def outcome_from_mailbox(self) -> bool:
        """Whether the outcome standing on this row was written by the sync.

        Both pages that draw an outcome ask this, because both used to draw
        every outcome as something a person recorded — and for the one line the
        mailbox writes itself that is a sentence nobody said. Read off the stored
        author rather than compared against a token in a template, so a renamed
        value cannot quietly turn every machine line back into a human's.
        """
        return self.outcome_by == OUTCOME_BY_MAILBOX

    #: What the journalist wrote back, oldest first — the order a conversation
    #: is read in. ``delete-orphan`` beside the database's own ``ON DELETE
    #: CASCADE``: a deleted letter takes its replies with it either way, so no
    #: journalist's words outlive the letter they answered, whether the row goes
    #: through the ORM or through a raw ``DELETE``.
    replies: Mapped[list["OutreachReply"]] = relationship(
        back_populates="letter",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="OutreachReply.received_at",
    )


class OutreachReply(Base):
    """One message a journalist sent back, filed against the letter it answers.

    Its own table rather than a column on :class:`Outreach`, because one letter
    can collect several: an answer, a follow-up question, the note two weeks
    later that the piece is running. A column would hold the last one and lose
    the rest, and the rest is the conversation.

    Everything here is stored as it arrived and **nothing is interpreted**. There
    is no "kind" or "sentiment" column: "danke, nichts für uns" and "schicken Sie
    mehr" are the same event to a matcher and opposite events to a PR consultant,
    so the only state a reply may set is ``ANTWORT`` — a human answered — and
    Absage or Veröffentlicht stay the consultant's reading (see
    :func:`newspulse.outreach.record_reply`).

    ``gmail_message_id`` is UNIQUE across the table rather than per letter: it is
    Google's id for one message in one mailbox, so a second row carrying it would
    be the same mail filed twice. That constraint is what makes the daily sync
    idempotent — a sweep that runs twice over the same mailbox stores nothing new
    and moves no timestamp.

    This is somebody else's data: a journalist's own words about a person who
    never agreed to be in RauteOS. Hence ``fetched_at`` beside ``received_at`` —
    when the mail was written and when this tool took a copy are two different
    facts, and a retention rule later needs the second one.
    """

    __tablename__ = "outreach_replies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    outreach_id: Mapped[int] = mapped_column(
        ForeignKey("outreach.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Google's id for this message. The idempotency key of the whole sync.
    gmail_message_id: Mapped[str] = mapped_column(
        String(120), nullable=False, unique=True
    )
    #: The display name off the ``From`` header; empty when the sender used none.
    from_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    from_email: Mapped[str] = mapped_column(String(320), nullable=False, default="")
    #: Gmail's own moment for the message, not this machine's clock: the reply
    #: that arrived on Saturday is dated Saturday even when it was read on Monday.
    received_at: Mapped[dt.datetime] = mapped_column(UTCDateTime(), nullable=False)
    #: The plain-text body, as sent. No HTML is ever stored — see
    #: ``gmail_link._plain_text``.
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    fetched_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )

    letter: Mapped["Outreach"] = relationship(back_populates="replies")

    @property
    def sender(self) -> str:
        """Who wrote it, in one line: the name when the header carried one, the
        address otherwise. Never both invented — an empty ``From`` reads as an
        empty line rather than as a guessed name."""
        return self.from_name or self.from_email


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
    __table_args__ = (
        # One unreleased draft per format per impulse, enforced rather than
        # assumed. newspulse.assets.store() looks for the draft it replaces and
        # then inserts, and the daily run writes formats from a background
        # worker: two writes that interleave between the read and the insert
        # leave two drafts of the same release on one impulse, and the page
        # renders both with no way to tell which one anybody meant. Partial, so
        # the released rows beside them stay untouched: those are the record of
        # what went out, and there can be several.
        Index(
            "ux_assets_angle_kind_unreleased",
            "angle_id",
            "kind",
            unique=True,
            sqlite_where=text("released_at IS NULL"),
            postgresql_where=text("released_at IS NULL"),
        ),
    )

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
    #: invented: :func:`newspulse.assets.write` copies it out of the profile field
    #: the format named and discards whatever the model answered, and a format that
    #: needs it will not be written without it.
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

    #: The standards this text was written under, on the same terms as
    #: :attr:`Angle.brain_version`: captured with the prompt, NULL for a text
    #: from before there was anything to stamp. A press release goes out under
    #: the client's name, so which version of the house's rules produced it is
    #: part of the record rather than a detail of the run.
    brain_version: Mapped[int | None] = mapped_column(Integer, nullable=True)

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



class ReportState(StrEnum):
    """Whether a report is still the tool's proposal or the agency's document.

    Two members and no third. Everything before a release is a draft the tool may
    overwrite; everything after it is a record of what a client was sent, and the
    same rule the outreach ledger applies to a letter applies here: once somebody
    has put the agency's name on it, it is never replaced.
    """

    ENTWURF = "entwurf"
    FREIGEGEBEN = "freigegeben"


class ReportFindingKind(StrEnum):
    """What sort of statement a finding makes about the month.

    Typed so a risk and a visibility finding are distinguishable without reading
    them: the consultant reviewing twelve findings before a jour fixe needs to see
    which two are the ones that cost something, and a colour on a card cannot be
    derived from prose.

    Four, and no more. Each corresponds to a question a client actually asks about
    a month, and a fifth would be a category invented to hold a sentence that did
    not fit the other four.
    """

    SICHTBARKEIT = "sichtbarkeit"  # how present the mandate was
    RISIKO = "risiko"              # something here gets more expensive if it stays
    WIRKUNG = "wirkung"            # this agency's own outreach produced coverage
    BOTSCHAFT = "botschaft"        # the mandate's own message carried, or did not


class Report(Base):
    """One mandate's period, read: the findings a consultant reviews and releases.

    A row per (client, period) rather than per generation. Generating July twice
    is one report drafted twice, not two Julys, and a second row would put two
    documents with the same date and different sentences in front of a client. The
    UNIQUE below makes that a schema guarantee rather than something ``store``
    remembers.

    ``note`` carries the period-level statement when there is no finding to make.
    A month with no coverage is a legitimate answer here, and it has to arrive as
    a sentence rather than as an empty list nobody can distinguish from a failure.
    """

    __tablename__ = "reports"
    __table_args__ = (
        UniqueConstraint(
            "client_id", "period_start", "period_end", name="uq_reports_client_period"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The window this report is about, ``start`` inclusive and ``end`` exclusive,
    #: exactly as :class:`newspulse.reporting.Period` holds it. Stored rather than
    #: derived from a month number: a report may cover a partial period, and a
    #: document that recomputed its own window would change what it said.
    period_start: Mapped[dt.datetime] = mapped_column(UTCDateTime(), nullable=False)
    period_end: Mapped[dt.datetime] = mapped_column(UTCDateTime(), nullable=False)
    state: Mapped[ReportState] = mapped_column(
        SAEnum(
            ReportState,
            values_callable=lambda enum: [m.value for m in enum],
            create_constraint=True,
            name="report_state",
        ),
        nullable=False,
        default=ReportState.ENTWURF,
        server_default=ReportState.ENTWURF.value,
    )
    generated_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )
    #: When a person put the agency's name on it. Null while it is a draft, and the
    #: difference is load-bearing: this is the artefact that goes to the client.
    released_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    #: Who released it. Follows ``Outreach.released_by`` and ``ClientFact.filled_by``:
    #: there are no user accounts here, so the interesting fact is that a person was
    #: in the loop. Empty means never released.
    released_by: Mapped[str] = mapped_column(
        String(80), nullable=False, default="", server_default=""
    )
    #: Why there is nothing to say, when there is nothing to say.
    note: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    #: The document as it read the moment it was released — figures, findings and
    #: the evidence under them, copied rather than referenced. Null while it is a
    #: draft, and written exactly once, by :func:`newspulse.report.release`.
    #:
    #: This is the one place in the feature where copying beats pointing, and it is
    #: the same reason the rest of it points: a *draft* must notice that its ground
    #: moved, so its evidence is ids resolved against the archive as it is. A
    #: released report is the artefact a client was sent, and re-deriving it from a
    #: live archive means a piece of coverage dismissed in October silently changes
    #: what the September document says it said. Both are honesty about the same
    #: rows; which one applies is decided by whether a human put the agency's name
    #: on it.
    snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    findings: Mapped[list["ReportFinding"]] = relationship(
        back_populates="report",
        cascade="all, delete-orphan",
        order_by="ReportFinding.id",
        lazy="selectin",
    )


class ReportFinding(Base):
    """One claim, what follows from it, and the ids of the rows underneath it.

    ``evidence_ids`` points at :class:`Analysis` rows, and it is never empty: a
    finding the model returned without evidence is discarded in
    :mod:`newspulse.report` before it reaches this table. That is the whole safety
    argument of the feature, so it is worth saying where the column is defined —
    the ids are attached by code from the metrics the claim cites, not quoted by
    the model, which cannot be trusted to remember which article a number came
    from.

    Pointing at analyses rather than copying headlines is what lets a finding go
    weak in public: an article that is later dismissed stops resolving, and the
    claim renders with the evidence that is left rather than with the ground it
    used to stand on.
    """

    __tablename__ = "report_findings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(
        ForeignKey("reports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[ReportFindingKind] = mapped_column(
        SAEnum(
            ReportFindingKind,
            values_callable=lambda enum: [m.value for m in enum],
            create_constraint=True,
            name="report_finding_kind",
        ),
        nullable=False,
    )
    #: One sentence: what is the case.
    claim: Mapped[str] = mapped_column(Text, nullable=False)
    #: What follows from it. The half a client pays for, and the half a dashboard
    #: cannot produce.
    consequence: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    evidence_ids: Mapped[list[int]] = mapped_column(
        MutableList.as_mutable(JSON),
        default=list,
        nullable=False,
        server_default=_EMPTY_JSON_ARRAY,
    )
    #: Whether the consultant kept it. Dropped findings stay as rows: what was
    #: proposed and rejected is part of how a report was arrived at, and a finding
    #: that vanished on a click cannot be argued about afterwards.
    kept: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )
    #: Why it was dropped, in the consultant's own words. Optional, because a
    #: finding that is simply wrong needs no essay — but a dropped finding that
    #: stays visible without a reason invites the same claim to be argued down
    #: again next month, and L8 cannot learn from a rejection nobody explained.
    #: Cleared when a dropped finding is taken back up.
    drop_reason: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    #: When a human last rewrote it. Null means the sentence is as generated, which
    #: is a thing the reviewer of a document is entitled to know.
    edited_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    report: Mapped["Report"] = relationship(back_populates="findings")


class VisibilityBand(StrEnum):
    """How far one question stands from the brand it is asked about.

    The band is the whole reason a question set measures anything. "Was macht
    Enpal" is trivia: the assistant was handed the answer in the question, and a
    set full of those reports a hundred per cent visibility for a mandate no
    buyer would ever find. "Welche Anbieter fuer Solaranlagen mit Speicher gibt
    es" is where a purchase starts, and it is the question the mandate is either
    in or not.

    So the four are ordered by distance from the brand, and only :attr:`MARKE`
    may name the client:

    * ``marke`` - the question names the company. What an assistant says about
      it to somebody who already knows it exists.
    * ``auswahl`` - a buyer choosing between named suppliers of this thing.
    * ``kategorie`` - the product category, no supplier named.
    * ``problem`` - the problem the category solves, the category not named
      either. The earliest point a buyer can be reached at all.

    A closed set, and there is deliberately no fifth member and no default. A
    proposal whose band nobody recognises is dropped in
    :mod:`newspulse.visibility` rather than filed under one of these, because a
    misfiled question changes a percentage the agency reports to a client.
    """

    MARKE = "marke"
    AUSWAHL = "auswahl"
    KATEGORIE = "kategorie"
    PROBLEM = "problem"


class VisibilityQuestion(Base):
    """One question a mandate is measured on, after a person accepted it.

    A row exists only once somebody clicked. :func:`newspulse.visibility.propose`
    returns candidates and stores none of them, for the reason ``rivals.py``
    states about competitors and this feature inherits unchanged: a wrong
    question silently changes a number the agency reports to a client, and the
    number reads exactly as well when it is wrong.

    ``accepted`` is therefore not "has been through the review" - a stored row is
    accepted by construction - it is whether the question is still in the set.
    Retiring one clears the flag instead of deleting the row, because
    :class:`VisibilityAnswer` points here: a deleted question would take every
    measurement it was ever part of with it, and the movement panel compares this
    week against a week whose questions have to still resolve.

    UNIQUE on (client, text) so the same wording cannot be accepted twice. Two
    identical questions are not two measurements, they are one question counted
    twice in a share.
    """

    __tablename__ = "visibility_questions"
    __table_args__ = (
        UniqueConstraint("client_id", "text", name="uq_visibility_question_client_text"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The question exactly as it will be put to a provider. No template, no
    #: placeholder: what is asked is what is stored, so the answer beside it can
    #: be read as the answer to this.
    text: Mapped[str] = mapped_column(Text, nullable=False)
    band: Mapped[VisibilityBand] = mapped_column(
        SAEnum(
            VisibilityBand,
            values_callable=lambda enum: [m.value for m in enum],
            create_constraint=True,
            name="visibility_band",
        ),
        nullable=False,
    )
    accepted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sql_text("1")
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )
    #: When a person put it into the set. Distinct from ``created_at`` because a
    #: retired question that is taken back up is accepted a second time, and the
    #: page says since when the set has looked the way it does.
    accepted_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class VisibilityRun(Base):
    """One measurement of one mandate: which providers were asked, which failed.

    ``providers_failed`` is the column the whole feature turns on. A provider
    that errored has no answer row for the questions it did not reach, and
    without this list that absence is indistinguishable from "the mandate was not
    named" - which is the one wrong number this feature could produce, because it
    is wrong in the direction a client would act on.

    ``answers_unread`` is the third state of the same distinction: the provider
    answered and the reading model could not read it. Nobody failed to answer, so
    the provider does not belong in the list above, and there is no row - the run
    has to say so itself.
    """

    __tablename__ = "visibility_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ran_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow, index=True
    )
    #: Every provider this run put the set to, whatever came back.
    providers_asked: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON),
        default=list,
        nullable=False,
        server_default=_EMPTY_JSON_ARRAY,
    )
    #: Those of them that could not answer. A subset of the above, never a
    #: separate vocabulary.
    providers_failed: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON),
        default=list,
        nullable=False,
        server_default=_EMPTY_JSON_ARRAY,
    )
    #: When the measurement stopped, whatever it produced. NULL while it is still
    #: running, and that is what stops a second sweep from spending the same set:
    #: the row is written before the first provider is asked, so a run in flight
    #: is visible to anybody else who looks.
    finished_at: Mapped[dt.datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )
    #: Answers that came back and could not be read. A provider that answered is
    #: not a provider that failed, so these cells are in neither ``answers`` nor
    #: ``providers_failed`` - and without the count, a run whose reading model was
    #: down is indistinguishable from a run nobody answered, while having cost
    #: every call those answers took. It is also what lets such a run hold the
    #: window instead of putting the whole set to the providers again on the next
    #: sweep.
    answers_unread: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sql_text("0")
    )

    answers: Mapped[list["VisibilityAnswer"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="VisibilityAnswer.id",
        lazy="selectin",
    )


class VisibilityAnswer(Base):
    """What one provider answered to one question, and what that answer says.

    ``answer`` is verbatim and stays verbatim. Every figure the page shows is
    computed from this column, so each of them resolves to something a person can
    open and read rather than trust - which is the difference between a claim an
    agency can make to a client and one it can only repeat. It is deliberately
    not run through :func:`newspulse.prose.plain`: that rule governs text this
    tool *writes*, and editing a measurement is falsifying it.

    ``position`` is a rank among the companies named in this one answer, not a
    ranking of the market. It is NULL exactly when ``named`` is false, and the
    CHECK below makes that a schema guarantee rather than an invariant three
    readers have to remember.
    """

    __tablename__ = "visibility_answers"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "question_id", "provider", name="uq_visibility_answer_cell"
        ),
        # 1-based, because the page prints it: "Position 2" reads as a rank and
        # "Position 1" as a rank; "Position 0" reads as a bug.
        CheckConstraint(
            "position IS NULL OR position >= 1", name="ck_visibility_answer_position"
        ),
        # An answer that does not name the mandate cannot rank it. Storing a
        # position beside named=0 would put a number on the page that the answer
        # underneath it does not support.
        #
        # Written as a bare boolean rather than "named = 1": SQLite stores the
        # column as 0/1 and reads either spelling, but a stricter dialect types
        # it as a real boolean and rejects the comparison against an integer
        # outright. This form is the one both understand.
        CheckConstraint(
            "named OR position IS NULL", name="ck_visibility_answer_unnamed_rank"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("visibility_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: RESTRICT rather than CASCADE, and the one place in this schema that is not
    #: a cascade: a question is retired by clearing its flag precisely so the
    #: answers it produced keep resolving. Deleting one would silently rewrite
    #: what past measurements said.
    question_id: Mapped[int] = mapped_column(
        ForeignKey("visibility_questions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    #: Which assistant answered. A plain string and not an enum: DEC-2 keeps the
    #: door open for a third provider, and the PRD says so in as many words - a
    #: further one is meant to be a definition, not a migration.
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    named: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Every company named in this answer, in the order they first appear. The
    #: ground ``position`` is a rank in, kept so the number can be checked rather
    #: than recomputed against an answer that is no longer being read the same way.
    companies: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON),
        default=list,
        nullable=False,
        server_default=_EMPTY_JSON_ARRAY,
    )
    #: Those of them that are this mandate's stored competitors. The intersection
    #: is what keeps an unrelated firm counting as market rather than as a rival.
    rivals: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON),
        default=list,
        nullable=False,
        server_default=_EMPTY_JSON_ARRAY,
    )
    #: What the model itself said it was going on. Never derived from the answer
    #: text: a model that cited nothing gets an empty list, because "we do not
    #: know what it read" and "it read these four things" are different facts.
    sources: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON),
        default=list,
        nullable=False,
        server_default=_EMPTY_JSON_ARRAY,
    )

    run: Mapped["VisibilityRun"] = relationship(back_populates="answers")
    question: Mapped["VisibilityQuestion"] = relationship(lazy="selectin")


#: The bounds of a crisis level. Five steps, because a consultant grades a
#: morning on a hand and not on a percentage, and because the arithmetic behind
#: it (:mod:`newspulse.crisis`) tops out at exactly five distinct outcomes.
CRISIS_LEVEL_MIN = 1
CRISIS_LEVEL_MAX = 5

#: How much of a declarer's name the row keeps. Eighty characters is the same
#: width :attr:`ClientFact.filled_by` uses, and it is a ceiling rather than a
#: validation rule: :func:`newspulse.crisis.declare` truncates to it, because a
#: long sign-in name must cost the tail of a name and never the declaration.
CRISIS_DECLARED_BY_MAX = 80


class Crisis(Base):
    """One declared crisis: when it began, how bad it is, who said so, when it ended.

    A crisis used to be a red card on Today — an ``Analysis`` with
    ``category = krise`` and nothing else. That is a *story*, not a state, and no
    state meant nothing in the tool could behave differently while one lasted.
    This row is the state, and it is the only condition under which the sweep
    changes its cadence.

    Three properties are load-bearing and each is a schema guarantee rather than
    something a caller has to remember:

    * **At most one open crisis per mandate.** A partial UNIQUE index over the
      rows with no ``closed_at`` — a second declaration therefore cannot create a
      second row even if two browser tabs press the button at the same second.
      :func:`newspulse.crisis.declare` hands the standing one back so the caller
      never has to see the ``IntegrityError``.
    * **A closed crisis always carries a reason.** That is what the CHECK
      enforces — one direction, not both: it cannot be closed without a reason,
      because "why did we stand this down" is the first question the review asks
      and an empty string answers it with silence. The other direction is a
      convention of :func:`newspulse.crisis.close`, which is the only writer of
      the pair, and not something a reader may infer from the column: a
      non-empty ``close_reason`` does not by itself mean a crisis is closed.
      ``closed_at IS NULL`` is what open means.
    * **The level is arithmetic, and the arithmetic is on the row.** The four
      counts it was computed from are stored beside it, so the number is
      checkable months later against coverage that has since grown. A level
      nobody can re-derive is a level nobody will trust in the hour they need to.

    ``last_swept_at`` is the tighter cadence's entire memory. It lives here and
    not in the scheduler thread, so a restart mid-crisis neither loses the crisis
    nor runs its sweep twice.
    """

    __tablename__ = "crises"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The piece of coverage the crisis was declared off. Required: a crisis
    #: without a trigger cannot be graded and cannot be explained.
    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Who declared it. A user name where the tool has one, otherwise the
    #: ``"mensch"`` token :attr:`ClientFact.filled_by` already uses — never a
    #: name nobody typed. DEC-1 turns on a *person* having decided, so the row
    #: has to be able to say that a person did.
    declared_by: Mapped[str] = mapped_column(
        String(CRISIS_DECLARED_BY_MAX), nullable=False
    )
    declared_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )
    #: NULL exactly while the crisis is open. It is what the partial unique index
    #: below is built on, and what the tighter cadence reads.
    closed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    close_reason: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )

    # --- The level, and the four counts behind it ------------------------------
    #
    # Computed by :func:`newspulse.crisis.severity` and stored together, because
    # the counts are what make the level checkable. A model that estimates a
    # crisis level returns a number nobody can re-derive, in exactly the hour
    # somebody wants to.
    level: Mapped[int] = mapped_column(
        Integer, nullable=False, default=CRISIS_LEVEL_MIN, server_default=text("1")
    )
    #: How many distinct outlets carry the story.
    outlet_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    #: How many stored pieces the story has at all — the denominator of the
    #: negative share, kept as an integer beside its numerator so no rounded
    #: percentage has to be trusted.
    article_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    #: How many of them read negative *for this mandate* (see :class:`Tonality`).
    negative_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    #: Whether any of the outlets is a national one (outlet tier 1).
    national: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    #: Whether the mandate is named in the feed-provided text of any of them.
    named: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )

    #: When the tighter cadence last read this mandate's sources. NULL means it
    #: never has, which is due immediately — a crisis declared at nine should not
    #: wait an hour for its first reading.
    last_swept_at: Mapped[dt.datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )

    client: Mapped["Client"] = relationship(lazy="selectin")
    article: Mapped["Article"] = relationship(lazy="selectin")

    __table_args__ = (
        CheckConstraint(
            f"level >= {CRISIS_LEVEL_MIN} AND level <= {CRISIS_LEVEL_MAX}",
            name="ck_crises_level_range",
        ),
        # Open means no reason, closed means a reason. Written with the bare
        # column rather than "closed_at IS NOT NULL AND ..." so both halves read
        # in the same direction as the sentence above them.
        CheckConstraint(
            "closed_at IS NULL OR close_reason <> ''", name="ck_crises_close_reason"
        ),
        # One open crisis per mandate, as a partial index: closed rows are
        # excluded, so a mandate may have had ten crises and be in none.
        #
        # Both dialect spellings, like the partial index on ``assets``. The
        # predicate is a dialect keyword rather than a portable argument, and the
        # one that is not recognised is silently dropped — which on a backend
        # this file did not name would leave a plain UNIQUE(client_id) behind and
        # forbid a mandate a second crisis for ever.
        Index(
            "uq_crises_one_open_per_client",
            "client_id",
            unique=True,
            sqlite_where=text("closed_at IS NULL"),
            postgresql_where=text("closed_at IS NULL"),
        ),
    )


__all__ = [
    "Crisis",
    "CRISIS_DECLARED_BY_MAX",
    "CRISIS_LEVEL_MIN",
    "CRISIS_LEVEL_MAX",
    "VisibilityBand",
    "VisibilityQuestion",
    "VisibilityRun",
    "VisibilityAnswer",
    "ReportState",
    "ReportFindingKind",
    "Report",
    "ReportFinding",
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
    "BrainOverride",
    "ClientFact",
    "OnboardingAnswer",
    "ANSWERED_BY_DEFAULT",
    "Contact",
    "OUTCOME_BY_MAILBOX",
    "ProfileProposal",
    "Outreach",
    "Asset",


    "OutreachReply",
    "OutreachState",
    "SILENT_AFTER_DAYS",
    "TopicHit",
    "MarketSignal",
    "PlanHook",
    "HookSource",
    "HookState",
    "SignalKind",
    "SignalOrigin",
    "GuideSource",
    "Run",
    "Setting",
    "DEFAULT_COUNTRY",
    "MIN_RELEVANCE",
    "visible_coverage",
    "SCORE_MIN",
    "SCORE_MAX",
]
