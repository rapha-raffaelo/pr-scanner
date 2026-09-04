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
    Date,
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
    # The two crisis formats (UHR-02). Written off a declared crisis rather than
    # an impulse, and the only two in the tool where minutes count. They live in
    # their own registry (:data:`newspulse.assets.CRISIS_FORMATS`) so the
    # impulse's format strip never offers them.
    HOLDING_STATEMENT = "holding_statement"
    KRISEN_QA = "krisen_qa"


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


#: The confidence floor beneath :func:`visible_coverage`'s main question. It is
#: not the question itself — ``is_relevant`` is — and treating it as such is what
#: let every rejection scored 1 or higher render as coverage.
MIN_RELEVANCE = 1


def visible_coverage():
    """The one condition every view of a client's coverage must apply.

    There were nine copies of ``relevance_score >= 1`` across as many modules, and
    adding a second reason to hide a row — a human dismissing it — would have meant
    finding all of them and never missing one. One predicate cannot drift, and a
    dismissed article cannot survive in the corner nobody remembered.

    ``is_relevant`` is the analyzer's own answer to "does this concern the
    mandate", and it was not asked here. The score stood in for it, on the
    reasoning that a relevance of 0 is how the model says no — true, but it is
    only how the model says no *emphatically*. Everything it rejected while
    scoring 1 to 4 came through and was rendered as coverage.

    Measured before this line was added, per mandate, visible rows before and
    after asking:

        Zalando           375 -> 375     Freedom24   462 -> 100
        Asos               12 ->  12     Qonto        68 ->  19
        Revolut             2 ->   2     Remexian     43 ->  22
        H&M               176 -> 175     Arrakis      27 ->   0

    Genuine coverage loses nothing — Zalando, Asos and Revolut are unchanged —
    and what goes is what the model had already rejected in writing. Arrakis
    reaching zero is not a loss either: every one of those 27 was a crypto
    article that matched a topic term, and the model said so 27 times.
    """
    from sqlalchemy import and_

    return and_(
        Analysis.is_relevant.is_(True),
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
    #: Their role at the masthead — "Leiter Ressort Banken und Versicherer",
    #: "Freie Autorin", "Chefredakteur". Distinct from ``beat``, which is the
    #: subject they cover: two people on the same beat are approached
    #: differently when one of them runs the desk, and that was the one thing
    #: the book could not say. Kept out of ``notes`` on purpose — a fact every
    #: entry has belongs in a field, not in prose nobody can sort by.
    position: Mapped[str] = mapped_column(String(200), nullable=False, default="")
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
        # One occasion per newsjack opportunity, for the same race the plan
        # hook's index settles: a double-clicked "Text schreiben" on the fast
        # lane's card is two threadpool requests that both read nothing and
        # both insert. Partial, because NULL is the value on nearly every row.
        Index(
            "ux_angles_newsjack",
            "newsjack_id",
            unique=True,
            sqlite_where=text("newsjack_id IS NOT NULL"),
            postgresql_where=text("newsjack_id IS NOT NULL"),
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
    #: The newsjack opportunity this occasion was opened from (UHR-05), on the
    #: same terms as :attr:`plan_hook_id`: a person clicked "Text schreiben" on
    #: the fast lane's card, and the texts written afterwards are that
    #: opportunity's texts — which is what lets the card and the mandate's
    #: archive say "dazu ist ein Text entstanden" and link to it.
    #: :class:`Asset` keeps hanging on the occasion, so a format needs to know
    #: nothing about where its occasion came from. NULL everywhere else, and
    #: ``SET NULL`` so deleting a weighed story cannot take a released text
    #: down with it. Indexed by ``ux_angles_newsjack`` above.
    newsjack_id: Mapped[int | None] = mapped_column(
        ForeignKey("newsjack_opportunities.id", ondelete="SET NULL"),
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
        # The same rule for the crisis texts, which hang on no angle: one
        # unreleased draft per format per crisis. The ``crisis_id IS NOT NULL``
        # half keeps the angle-anchored rows out of it, since NULLs would not
        # collide anyway and the predicate says so out loud.
        Index(
            "ux_assets_crisis_kind_unreleased",
            "crisis_id",
            "kind",
            unique=True,
            sqlite_where=text("released_at IS NULL AND crisis_id IS NOT NULL"),
            postgresql_where=text("released_at IS NULL AND crisis_id IS NOT NULL"),
        ),
        # A text hangs on the impulse it argues or on the crisis it answers,
        # never on nothing: a stored text nothing can trace back to its occasion
        # is a text nothing can check.
        CheckConstraint(
            "angle_id IS NOT NULL OR crisis_id IS NOT NULL",
            name="ck_assets_anchor",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The impulse this text argues — NULL exactly for the crisis texts, which
    #: argue no position and hang on ``crisis_id`` instead. The CHECK above
    #: guarantees one of the two anchors is always set.
    angle_id: Mapped[int | None] = mapped_column(
        ForeignKey("angles.id", ondelete="CASCADE"), nullable=True, index=True
    )
    #: The declared crisis this text answers (UHR-02). CASCADE like the angle's:
    #: a crisis text whose crisis is gone cannot be explained, and crises are
    #: only ever deleted with their whole mandate anyway.
    crisis_id: Mapped[int | None] = mapped_column(
        ForeignKey("crises.id", ondelete="CASCADE"), nullable=True, index=True
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



class Standing(StrEnum):
    """Whether a mandate has something to say on a market story it is not in.

    The question the positioning drafts never ask, and the difference between a
    contribution and an embarrassment (UHR-04). Answered against profile, guide
    and archive, and the set is closed at exactly three:

    * ``belegt`` — the mandate can point at something stored: a profile fact, a
      guide line, its own past coverage of the subject. The only answer that
      produces an opportunity.
    * ``duenn`` — plausible, but nothing stored backs it. Spelled without the
      umlaut the way ``ungeprueft`` and ``veroeffentlicht`` are.
    * ``keins`` — the mandate has nothing to do with the subject.

    There is deliberately no fourth member and no default: an unreadable verdict
    stores nothing at all (the next scan asks again), because a misfiled
    standing either spends a consultant's morning or silences a real opening.
    """

    BELEGT = "belegt"
    DUENN = "duenn"
    KEINS = "keins"


class NewsjackOpportunity(Base):
    """One market story weighed for one mandate: its origin, window and standing.

    A row is written at most once per story per mandate, and it is written for
    *every* verdict, not only the good one. A ``belegt`` row is the opportunity
    the Today page shows; a ``duenn`` or ``keins`` row is the rejection, kept
    with its reason — both because "warum schlägt das Werkzeug hier nichts vor"
    deserves an answer, and because the stored row is what stops the next scan
    from paying for the same model call again.

    ``article_id`` is the story's **origin** — its earliest piece, resolved by
    :func:`newspulse.stories.origin` — and the UNIQUE over (client, article) is
    what makes "dieselbe Story je Mandat höchstens einmal" a schema guarantee:
    a second scan re-clusters the same rows to the same origin and cannot file
    a second row even if its pre-check raced another process.

    ``window_ends_at`` is stored rather than derived at read, so the promise
    "expired after N hours from the origin, whether or not a run ever happened
    again" survives a later change to the configured width: the window an
    opportunity was created under is the window it expires under. Expiry itself
    is a comparison against the clock, never a job — a row nothing ever touches
    again still stops being shown on time.

    ``dismissed_at`` is the fast lane's stand-down (UHR-05): a person waving a
    story off for this mandate. Stamped, not deleted, for the same reason a
    rejection is stored — the row is what keeps the story from coming back.
    """

    __tablename__ = "newsjack_opportunities"
    __table_args__ = (
        UniqueConstraint(
            "client_id", "article_id", name="uq_newsjack_client_article"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The origin article: the story's earliest piece. CASCADE like the crisis
    #: trigger's, and required for the same reason — an opportunity without its
    #: origin cannot say who had the story first or when its window ends.
    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    standing: Mapped[Standing] = mapped_column(
        SAEnum(
            Standing,
            values_callable=lambda enum: [m.value for m in enum],
            create_constraint=True,
            name="newsjack_standing",
        ),
        nullable=False,
    )
    #: The model's one sentence: what the standing rests on (``belegt``), or why
    #: there is none (``duenn``/``keins``). The one thing here a model writes.
    reason: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    #: Distinct outlets carrying the story when the verdict was made — the
    #: number the card shows beside "wer hatte es zuerst".
    pickup_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    #: When the window closes: origin ``published_at`` plus the configured
    #: hours, fixed at creation. Read against the clock, never against a run.
    window_ends_at: Mapped[dt.datetime] = mapped_column(UTCDateTime(), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )
    #: When a person waved the story off for this mandate. NULL while it stands.
    dismissed_at: Mapped[dt.datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )
    #: The standards the standing check was composed under, on the same terms as
    #: :attr:`Angle.brain_version`: captured with the prompt, NULL only for a
    #: row from before there was anything to stamp.
    brain_version: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
    )

    client: Mapped["Client"] = relationship(lazy="selectin")
    article: Mapped["Article"] = relationship(lazy="selectin")


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


class CrisisDismissal(Base):
    """One proposal a person stood down without declaring anything (UHR-03).

    Its own table rather than an instantly-closed :class:`Crisis` row, because
    the two are opposite statements. A crisis row says a person decided the
    mandate *was* in one; a dismissal says a person looked at the same offer and
    decided it was not. Writing the second as the first would put a phantom
    crisis into the mandate's record — the Krise tab would appear, the
    chronology would show a crisis nobody declared — and every reader of
    ``crises`` would have to know a secret marker to tell them apart.

    What it silences is the *story*, not the row: :func:`newspulse.crisis.propose`
    reads these triggers through the same clustering the closed crises use, so
    the pickups of a dismissed wave stop re-offering it under a different
    headline. DEC-1's whole rationale — a false alarm costs one click — is this
    row.

    ``(client_id, article_id)`` is UNIQUE so a double click, a second tab and a
    replayed POST all land on the same dismissal rather than growing copies.
    """

    __tablename__ = "crisis_dismissals"
    __table_args__ = (
        UniqueConstraint(
            "client_id", "article_id", name="uq_crisis_dismissals_once"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The proposal's trigger article. CASCADE like the crisis's own trigger: a
    #: dismissal of coverage that no longer exists silences nothing and explains
    #: nothing, so it does not outlive it.
    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Who pressed "Verwerfen" — the same discipline as ``Crisis.declared_by``:
    #: a user name where the tool has one, the ``"mensch"`` token otherwise,
    #: never a name nobody typed.
    dismissed_by: Mapped[str] = mapped_column(
        String(CRISIS_DECLARED_BY_MAX), nullable=False
    )
    dismissed_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )

    article: Mapped["Article"] = relationship(lazy="selectin")


#: The rungs of the ladder, as the arithmetic and the interface both name them.
class ReputationState(StrEnum):
    """How a mandate stands, counted from stored rows and never estimated.

    Five rungs, and the order they are declared in *is* the ladder —
    :func:`newspulse.reputation.rank` reads this class, so a rung inserted in
    the wrong place would silently reorder the comparison the open-crisis floor
    relies on.

    ``ISSUE`` is the one rung no sum over a single day can reach, and it is not
    reached by one: :func:`newspulse.reputation.measure` sets it where the
    arithmetic says Beobachtung and the stored series says this mandate already
    stood above ruhig on another day this week — a matter being carried rather
    than a bad morning. What the series cannot yet say is *which* matter; the
    issue register (RIS-02) will name it, and it will name it on this rung.
    """

    RUHIG = "ruhig"
    BEOBACHTUNG = "beobachtung"
    ISSUE = "issue"
    RISIKO = "risiko"
    KRISE = "krise"


class ReputationReading(Base):
    """One mandate's state on one day: the rung, the four inputs, and when.

    The row exists so the state has a *history*, which is the whole difference
    between this and the red card on Today that expires at midnight. Two things
    fall out of a stored series without any further work:

    * the direction, because there is a run of previous days to compare against;
    * the deviation, because a mandate can be measured against its own median
      instead of against a threshold that would mean the same thing for a
      municipal utility and for a listed group.

    **One reading per mandate and day** — the UNIQUE below, not a convention.
    The sweep runs once a morning today, but a manual run, a redeploy or a
    second scheduler tick would otherwise leave two rows for the same day, and
    every median and every trend read over that series would double-weight
    whichever day happened to be swept twice.
    :func:`newspulse.reputation.record` updates the standing row instead.

    **The four inputs sit beside the rung**, exactly as they do on
    :class:`Crisis` and for exactly the same reason: a rung nobody can re-derive
    is a rung nobody will defend in front of a client. ``articles`` and
    ``negative`` are kept as two integers rather than as a rounded share, so the
    fraction is checkable rather than merely plausible.

    ``articles = 0`` is a *reading*, not a missing one: a mandate nobody wrote
    about is quiet, and the row is what says so.
    """

    __tablename__ = "reputation_readings"
    __table_args__ = (
        UniqueConstraint("client_id", "day", name="uq_reputation_reading_per_day"),
        CheckConstraint(
            "negative >= 0 AND articles >= negative",
            name="ck_reputation_reading_share",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The *local* day the reading is for — the same day the Heute page is keyed
    #: on. A UTC day would file a reading taken at 01:00 Berlin time under the
    #: previous day, and the band and the coverage under it would then disagree
    #: about what day it is.
    day: Mapped[dt.date] = mapped_column(Date(), nullable=False, index=True)
    state: Mapped[ReputationState] = mapped_column(
        SAEnum(
            ReputationState,
            values_callable=lambda enum: [m.value for m in enum],
            create_constraint=True,
            name="reputation_state",
        ),
        nullable=False,
        default=ReputationState.RUHIG,
        server_default=ReputationState.RUHIG.value,
    )

    # --- The four inputs, and the sum they produced ---------------------------
    #: How many independent outlets carry the strongest negative story of the
    #: window. Story-scoped on purpose: two outlets on *the same* thing is what
    #: corroboration means, and two outlets on two unrelated stories is not.
    outlets: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    #: The reach class, as the one bit the ranking in ``outlet_tiers.toml``
    #: actually decides: whether any of the negative coverage ran nationally.
    national: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    #: The denominator of the negative share: everything visible about this
    #: mandate in the window. Zero means nothing was written, which is a
    #: statement and not a gap.
    articles: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    #: Its numerator: how many of those read negative for the mandate.
    negative: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    #: Whether the mandate is named in the feed-provided text of any negative
    #: piece. No body is ever fetched, here or anywhere else.
    named: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    #: The sum the rung was read off. Stored rather than recomputed on render,
    #: because it is what the direction and the median are counted over — a
    #: series recomputed from today's coverage would silently rewrite its own
    #: history as articles age out of the window.
    points: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    #: When the reading was taken. The acceptance asks for it by name, and it is
    #: the one thing ``day`` cannot say: a reading updated at 18:00 by a second
    #: run is a different statement from the one taken at 06:10.
    computed_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )

    #: Lazy, unlike the eager relationships on :class:`Crisis` and the rest.
    #: Those are loaded because the page renders them; nothing renders a
    #: reading's mandate — the band already holds the :class:`Client` it asked
    #: for the reading about — so eager-loading it would buy a second query per
    #: statement on the busiest page in the tool and hand back an object the
    #: caller has.
    client: Mapped["Client"] = relationship()


#: The scale Wahrscheinlichkeit and Wirkung are set on. Five steps, the same
#: hand a crisis level is graded on (:data:`CRISIS_LEVEL_MIN`), because the two
#: numbers meet on one heatmap and a 1-5 next to a 1-10 would make the field
#: unreadable. NULL is a value of its own — "noch nicht gesetzt" — and the
#: heatmap gives it a named column rather than a corner of the field.
ISSUE_SCALE_MIN = 1
ISSUE_SCALE_MAX = 5


class IssueStatus(StrEnum):
    """Where one issue stands with the person who owns it.

    ``ESKALIERT`` is not a second kind of closed: the matter did not end, it
    became a crisis, and the row stays readable with all its signals because it
    is the crisis's prehistory. ``GESCHLOSSEN`` always carries a reason — the
    CHECK on :class:`Issue` holds that the way it does on :class:`Crisis`.
    """

    OFFEN = "offen"
    ESKALIERT = "eskaliert"
    GESCHLOSSEN = "geschlossen"


class Issue(Base):
    """The thing that gets three weeks old: one repeated matter, as one row.

    Until now the same accusation on Monday and on Friday was two cards on two
    days. This row is the object between the daily card and the declared
    crisis: it has an age, a last movement and a growing count of attached
    signals, and that is what "something is growing" is made of.

    Three disciplines are carried on the row rather than remembered by callers:

    * **A person opened it and a person grades it.** DEC-3 locked "das Werkzeug
      schlägt vor, ein Mensch eröffnet", so ``opened_by`` names the person who
      accepted the proposal. ``probability`` and ``impact`` are suggested by
      arithmetic and *set* by a person, and ``probability_set_by`` /
      ``impact_set_by`` say who — a model-set Eintrittswahrscheinlichkeit looks
      like a measurement and is an opinion. NULL means nobody has set the value
      yet, and the heatmap shows that as a named column, never as the origin of
      the field.
    * **A closed issue carries its reason**, the same CHECK the crisis has: an
      empty answer to "why did we stop watching this" is silence three months
      later. The row and its signals stay readable after.
    * **Escalation is a handover, not an end.** ``crisis_id`` points at the
      crisis this issue became, so the crisis's chronology can begin where the
      issue began rather than on the day somebody pressed the button.

    ``opened_at`` is the day the *matter* began — the earliest attached
    signal's own date, not the moment of the click — because the age on the
    register row and the start of an escalated crisis's chronology are both
    statements about the matter, and the click is a statement about the person.
    ``last_moved_at`` moves with the signals for the same reason.
    """

    __tablename__ = "issues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The matter in one line — the lead headline of the repetition it was
    #: opened from, editable by the person who owns it.
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    #: Frühindikatoren, one per line. Free text a person maintains: what to
    #: watch for before the matter grows, not something a model fills.
    early_indicators: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    owner: Mapped[str] = mapped_column(
        String(CRISIS_DECLARED_BY_MAX), nullable=False, default="", server_default=""
    )
    status: Mapped[IssueStatus] = mapped_column(
        SAEnum(
            IssueStatus,
            values_callable=lambda enum: [m.value for m in enum],
            create_constraint=True,
            name="issue_status",
        ),
        nullable=False,
        default=IssueStatus.OFFEN,
        server_default=IssueStatus.OFFEN.value,
    )
    #: When the matter began: the earliest attached signal's own date.
    opened_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )
    #: The person who accepted the proposal — the same discipline as
    #: ``Crisis.declared_by``: a user name where the tool has one, the
    #: ``"mensch"`` token otherwise, never a name nobody typed.
    opened_by: Mapped[str] = mapped_column(
        String(CRISIS_DECLARED_BY_MAX), nullable=False
    )
    #: When the matter last moved: the newest attached signal's own date.
    last_moved_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )

    # --- The two graded values, each with the person who set it ---------------
    probability: Mapped[int | None] = mapped_column(Integer, nullable=True)
    probability_set_by: Mapped[str] = mapped_column(
        String(CRISIS_DECLARED_BY_MAX), nullable=False, default="", server_default=""
    )
    impact: Mapped[int | None] = mapped_column(Integer, nullable=True)
    impact_set_by: Mapped[str] = mapped_column(
        String(CRISIS_DECLARED_BY_MAX), nullable=False, default="", server_default=""
    )

    closed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    close_reason: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    closed_by: Mapped[str] = mapped_column(
        String(CRISIS_DECLARED_BY_MAX), nullable=False, default="", server_default=""
    )
    #: The crisis this issue became, once it escalated. SET NULL rather than
    #: CASCADE: an issue outlives the crisis row the way its signals do — the
    #: prehistory does not vanish because the crisis record was deleted.
    crisis_id: Mapped[int | None] = mapped_column(
        ForeignKey("crises.id", ondelete="SET NULL"), nullable=True
    )

    signals: Mapped[list["IssueSignal"]] = relationship(
        back_populates="issue", lazy="selectin", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            f"probability IS NULL OR (probability >= {ISSUE_SCALE_MIN} "
            f"AND probability <= {ISSUE_SCALE_MAX})",
            name="ck_issues_probability_range",
        ),
        CheckConstraint(
            f"impact IS NULL OR (impact >= {ISSUE_SCALE_MIN} "
            f"AND impact <= {ISSUE_SCALE_MAX})",
            name="ck_issues_impact_range",
        ),
        # The same one-direction CHECK the crisis carries: it cannot be closed
        # without a reason. The other direction is a convention of
        # :func:`newspulse.issues.close`, the only writer of the pair.
        CheckConstraint(
            "closed_at IS NULL OR close_reason <> ''", name="ck_issues_close_reason"
        ),
    )


class IssueSignal(Base):
    """One signal hanging on one issue, with the reason it hangs there.

    A signal is either a stored article or a stored market signal — exactly one,
    and the CHECK holds that. Never free text: a signal that does not resolve to
    a stored row is not evidence of anything.

    ``reason`` is the load-bearing column. DEC-4 locked "mechanisch gefundene
    Kandidaten, das Modell entscheidet" — and the rule that came with it is that
    an assignment nobody can justify is not stored. The CHECK refuses an empty
    reason at the schema, so the rule survives every future writer of this
    table, not only the one that was reviewed.

    ``attached_by`` distinguishes the model's assignments (the ``"modell"``
    token) from a person's, the way ``ClientFact.filled_by`` does: the register
    shows who hung a signal on the row, because a model's one-sentence reason
    reads differently from a consultant's.
    """

    __tablename__ = "issue_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    issue_id: Mapped[int] = mapped_column(
        ForeignKey("issues.id", ondelete="CASCADE"), nullable=False, index=True
    )
    article_id: Mapped[int | None] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), nullable=True
    )
    signal_id: Mapped[int | None] = mapped_column(
        ForeignKey("market_signals.id", ondelete="CASCADE"), nullable=True
    )
    #: Why this belongs to this issue — stored, so every assignment is readable
    #: afterwards. Never empty; see the CHECK.
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    #: ``"modell"`` for a DEC-4 assignment, a person's name otherwise.
    attached_by: Mapped[str] = mapped_column(
        String(CRISIS_DECLARED_BY_MAX), nullable=False
    )
    attached_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )
    #: The signal's own date — the article's publication, or the market
    #: signal's stated date. What the issue's age and last movement are read
    #: from, because they are statements about the matter and not about when
    #: the tool noticed it.
    happened_at: Mapped[dt.datetime] = mapped_column(UTCDateTime(), nullable=False)

    issue: Mapped["Issue"] = relationship(back_populates="signals")
    article: Mapped["Article | None"] = relationship(lazy="selectin")
    market_signal: Mapped["MarketSignal | None"] = relationship(lazy="selectin")

    __table_args__ = (
        # Exactly one of the two targets, spelled as the boolean it is.
        CheckConstraint(
            "(article_id IS NULL) <> (signal_id IS NULL)",
            name="ck_issue_signals_one_target",
        ),
        CheckConstraint("reason <> ''", name="ck_issue_signals_reason"),
        # The same piece hangs on the same issue once. NULLs do not collide in
        # a UNIQUE, so the two constraints stay out of each other's way.
        UniqueConstraint("issue_id", "article_id", name="uq_issue_signals_article"),
        UniqueConstraint("issue_id", "signal_id", name="uq_issue_signals_signal"),
    )


class IssueDismissal(Base):
    """One issue proposal a person waved off (DEC-3's one-click false positive).

    The same posture as :class:`CrisisDismissal` and for the same reason:
    "verwerfen lässt dieselbe Wiederholung nicht erneut vorschlagen". Keyed on
    the proposal's lead article; :func:`newspulse.issues.propose` reads these
    through the same clustering the proposals come from, so the whole repeated
    story stops being offered, not merely the one headline that led it.

    ``(client_id, article_id)`` is UNIQUE so a double click, a second tab and a
    replayed POST all land on the same dismissal.
    """

    __tablename__ = "issue_dismissals"
    __table_args__ = (
        UniqueConstraint("client_id", "article_id", name="uq_issue_dismissals_once"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dismissed_by: Mapped[str] = mapped_column(
        String(CRISIS_DECLARED_BY_MAX), nullable=False
    )
    dismissed_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )


class StakeholderLevel(StrEnum):
    """How strongly a group is touched, or how much weight it carries.

    Three steps and no number: the stakeholder map is read in a hallway on the
    morning something breaks, and "hoch" is a word a consultant defends where a
    3.7 is a figure somebody computed once and nobody can re-derive. A closed
    set, so a proposal whose level nobody recognises is dropped in
    :mod:`newspulse.stakeholders` rather than filed under a guess.
    """

    HOCH = "hoch"
    MITTEL = "mittel"
    NIEDRIG = "niedrig"


#: How much of a contact name or a channel a map row keeps. A ceiling rather
#: than a validation rule, the same trade :data:`CRISIS_DECLARED_BY_MAX` makes:
#: an over-long line costs its tail, never the row. Named because the truncation
#: in :mod:`newspulse.stakeholders` and the form's ``maxlength`` have to agree —
#: five copies of a literal drift the first time one of them is raised.
STAKEHOLDER_TEXT_MAX = 200


class Stakeholder(Base):
    """One group on a mandate's standing stakeholder map (RIS-03).

    The map hangs on the *mandate*, not on an issue: who the neighbours of a
    site are and which association speaks for the industry does not change with
    the occasion, and a map reinvented per incident is half wrong per incident.
    What an issue gets is a **selection** from this table
    (:class:`StakeholderSelection`), never rows of its own.

    ``set_by`` is the discipline the profile already keeps for every researched
    value: each row says who put it there — the ``"modell"`` token for a
    proposed row, a person's name once a person has touched it — and a row a
    person set is never overwritten by a proposal
    (:func:`newspulse.stakeholders.propose_card` skips every standing row).

    ``contact`` empty is the most important row of the map, not a blank cell:
    the page renders it as a named gap with the link to where it is filled in,
    the way the missing crisis contact already reads on the crisis page.
    """

    __tablename__ = "stakeholders"
    __table_args__ = (
        # One row per group per mandate: proposing the same group twice must
        # update nothing and add nothing — the standing row is the answer.
        UniqueConstraint("client_id", "group_name", name="uq_stakeholders_group"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Who the group is — "Anwohner am Standort", "Branchenverband", a name a
    #: reader recognises. ``group_name`` rather than ``group``: the bare word is
    #: an SQL keyword and would need quoting in every raw statement for ever.
    group_name: Mapped[str] = mapped_column(Text, nullable=False)
    #: How this group is touched by the mandate, in stored prose. This is the
    #: line the issue selection's one-sentence "was sie wissen will" may rest
    #: on — the stored Angabe that keeps that sentence from inventing anything.
    betroffenheit: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    einfluss: Mapped[StakeholderLevel] = mapped_column(
        SAEnum(
            StakeholderLevel,
            values_callable=lambda enum: [m.value for m in enum],
            create_constraint=True,
            name="stakeholder_level",
        ),
        nullable=False,
        default=StakeholderLevel.MITTEL,
        server_default=StakeholderLevel.MITTEL.value,
    )
    #: The named person this group is reached through. Empty is a *named gap*
    #: on the page, never invented: a proposal writes no contact at all —
    #: a guessed name would be called on the one evening it matters.
    contact: Mapped[str] = mapped_column(
        String(STAKEHOLDER_TEXT_MAX), nullable=False, default="", server_default=""
    )
    #: How the group is reached: a channel, not an address book entry.
    channel: Mapped[str] = mapped_column(
        String(STAKEHOLDER_TEXT_MAX), nullable=False, default="", server_default=""
    )
    #: Who set this row: the ``"modell"`` token for a proposal, a person's name
    #: after any human edit. The map shows it on every line.
    set_by: Mapped[str] = mapped_column(
        String(CRISIS_DECLARED_BY_MAX), nullable=False
    )
    set_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )
    #: The standards a *proposed* row's prose was written under, on the same
    #: terms as :attr:`Angle.brain_version`: captured when the prompt is
    #: composed. NULL for a row a person wrote — their Betroffenheit is their
    #: own text, and stamping it would claim a model call that never happened.
    brain_version: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
    )

    @property
    def has_contact(self) -> bool:
        """Whether a person is named. The page asks, because the empty state is
        a named gap with a link, never a blank cell."""
        return bool(self.contact.strip())


class StakeholderSelection(Base):
    """One group of the standing map, selected for one issue or one crisis.

    A selection points into :class:`Stakeholder` rather than copying it — the
    map is maintained in one place, and a phone number corrected there is
    corrected under every open issue at once.

    ``reason`` is the price of admission, the same rule ``issue_signals``
    holds: a group selected without a stored sentence why this occasion touches
    it is not stored at all, and the CHECK keeps that against every future
    writer. ``info_need`` may be empty — the one sentence about what the group
    wants to know rests on stored lines only, and where nothing stored supports
    one, the honest row carries none rather than an invented Betroffenheit.

    ``position``/``position_set_by`` carry the order the groups should learn
    in. The proposal writes the ``"modell"`` token, which is what renders the
    order as an *Empfehlung*; a person resorting the list writes their own name
    over every row, and from then on the stored order is the person's — it
    hangs on law, contract and relationship, of which the tool sees only part.
    """

    __tablename__ = "stakeholder_selections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    issue_id: Mapped[int | None] = mapped_column(
        ForeignKey("issues.id", ondelete="CASCADE"), nullable=True, index=True
    )
    crisis_id: Mapped[int | None] = mapped_column(
        ForeignKey("crises.id", ondelete="CASCADE"), nullable=True, index=True
    )
    #: CASCADE: a map row a person removed takes its selections with it — a
    #: selection of a group that no longer exists explains nothing.
    stakeholder_id: Mapped[int] = mapped_column(
        ForeignKey("stakeholders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Why this occasion touches this group. Never empty; see the CHECK.
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    #: What this group wants to know, in one sentence resting on stored lines.
    #: Empty where nothing stored supports one — an omission, never a guess.
    info_need: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    #: 1-based rank in the order the groups should learn. The recommendation
    #: until a person resorts; the person's order afterwards.
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    #: ``"modell"`` while the order is the recommendation, the person's name
    #: once a human has sorted it — which is the order that is *kept*.
    position_set_by: Mapped[str] = mapped_column(
        String(CRISIS_DECLARED_BY_MAX), nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )
    #: The standards the reason and the information need were written under —
    #: model prose a consultant telephones by, which is exactly the kind of
    #: text the stamp exists for. NULL never happens through
    #: :func:`newspulse.stakeholders.select_for`, the only writer.
    brain_version: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
    )

    stakeholder: Mapped["Stakeholder"] = relationship(lazy="selectin")

    __table_args__ = (
        # A selection hangs on exactly one occasion — an issue or a crisis.
        CheckConstraint(
            "(issue_id IS NULL) <> (crisis_id IS NULL)",
            name="ck_stakeholder_selections_one_anchor",
        ),
        CheckConstraint("reason <> ''", name="ck_stakeholder_selections_reason"),
        # 1-based like the sentence it renders: "an erster Stelle" is 1.
        CheckConstraint("position >= 1", name="ck_stakeholder_selections_position"),
        # The same group stands in one occasion's list once. NULLs do not
        # collide, so the two constraints stay out of each other's way.
        UniqueConstraint(
            "issue_id", "stakeholder_id", name="uq_stakeholder_selections_issue"
        ),
        UniqueConstraint(
            "crisis_id", "stakeholder_id", name="uq_stakeholder_selections_crisis"
        ),
    )


# --- Szenarien, Auslöser und Reaktionsoptionen (RIS-04) ----------------------------


class ScenarioKind(StrEnum):
    """The three courses an issue can take, and there are exactly three.

    Not a free label: a page that can hold four scenarios holds four essays,
    and the value of "bester, wahrscheinlicher, schlechtester" is that the
    reader knows what each column is for before reading a word of it. The
    UNIQUE on :class:`Scenario` holds one row per kind per issue.
    """

    BESTER = "bester"
    WAHRSCHEINLICHER = "wahrscheinlicher"
    SCHLECHTESTER = "schlechtester"


class ScenarioLikelihood(StrEnum):
    """How likely a course is, **as a word and never as a percentage**.

    A percentage out of a model claims an accuracy that does not exist, and it
    is precisely that number which gets quoted back in the meeting four weeks
    later. A closed set of four words cannot be quoted as a measurement, and a
    value outside it is dropped in :mod:`newspulse.scenarios` rather than
    filed under a guess.
    """

    UNWAHRSCHEINLICH = "unwahrscheinlich"
    MOEGLICH = "möglich"
    WAHRSCHEINLICH = "wahrscheinlich"
    SEHR_WAHRSCHEINLICH = "sehr wahrscheinlich"


class TriggerCondition(StrEnum):
    """The closed set of conditions a scenario trigger may be built from (DEC-5).

    DEC-5 locked option A: "nur maschinell prüfbare Bedingungen". Every member
    here resolves to stored rows the sweep can actually read —

    * ``ZWEITES_MEDIUM`` — a second independent outlet carries the matter;
    * ``LEITMEDIUM`` — an outlet of the top reach tier carries it;
    * ``MANDAT_IN_UEBERSCHRIFT`` — the mandate is named in a headline;
    * ``MEDIENANFRAGE`` — a journalist wrote into the connected mailbox;
    * ``MANAGEMENT_GENANNT`` — a person named in the profile's management line
      appears in the coverage.

    A trigger that is only well phrased is never fired and is therefore not a
    trigger, which is why free text is not a member of this enum and a
    scenario whose triggers all fall outside it is not stored at all.
    """

    ZWEITES_MEDIUM = "zweites_medium"
    LEITMEDIUM = "leitmedium"
    MANDAT_IN_UEBERSCHRIFT = "mandat_in_ueberschrift"
    MEDIENANFRAGE = "medienanfrage"
    MANAGEMENT_GENANNT = "management_genannt"


class ResponseSpeed(StrEnum):
    """How fast the recommendation says to move, from a fixed set.

    "Schnell" and "sofort" are the same word to a model and four hours apart to
    an agency, so the recommendation names one of six and nothing else.
    ``KEINE`` is a member like any other: the most expensive mistake in this
    trade is the statement that gives a matter the publicity it did not yet
    have.
    """

    SOFORT = "sofort"
    EINE_STUNDE = "innerhalb einer Stunde"
    HEUTE = "heute"
    VIERUNDZWANZIG_STUNDEN = "innerhalb von 24 Stunden"
    VORBEREITEN = "vorbereiten und beobachten"
    KEINE = "keine Reaktion"


class EscalationPotential(StrEnum):
    """How much a response option could grow the matter. Three words, no number.

    Its own set rather than :class:`StakeholderLevel`'s: the two answer
    different questions and share only their spelling, and a scenario table
    reaching into the stakeholder map's enum would tie one feature's closed set
    to another's the first time either grows a fourth step.
    """

    HOCH = "hoch"
    MITTEL = "mittel"
    NIEDRIG = "niedrig"


#: How much of a response option's one-line label is kept. The same ceiling
#: trade :data:`STAKEHOLDER_TEXT_MAX` makes: an over-long line costs its tail,
#: never the row. The *model* writes this column, so the cap cannot live only
#: in a form's ``maxlength``.
RESPONSE_LABEL_MAX = 200

#: How many response options an issue must carry for the set to be stored at
#: all, per the acceptance. Three, and one of them is always "nicht reagieren"
#: — a tool that can only propose acting proposes acting.
RESPONSE_OPTIONS_MIN = 3


class Scenario(Base):
    """One of an issue's three courses: what could happen next (RIS-04).

    A scenario and never a forecast, and the difference is carried in three
    places rather than in a caption: ``likelihood`` is a word from a closed set
    and can never be a percentage; the narrative has to read as a scenario or
    :mod:`newspulse.scenarios` refuses it; and every row carries at least one
    machine-checkable trigger, without which it is not stored — an essay about
    the future is not a scenario.

    ``groups`` is the selection of affected stakeholders, pointing into the
    mandate's standing map (:class:`Stakeholder`) exactly as
    :class:`StakeholderSelection` does: the map is maintained in one place, and
    a group invented beside it would be a second map half wrong per incident.
    """

    __tablename__ = "scenarios"
    __table_args__ = (
        # Three courses, one row each. A second "schlechtester" is not a
        # fourth scenario, it is the same question answered twice.
        UniqueConstraint("issue_id", "kind", name="uq_scenarios_kind"),
        CheckConstraint("narrative <> ''", name="ck_scenarios_narrative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    issue_id: Mapped[int] = mapped_column(
        ForeignKey("issues.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[ScenarioKind] = mapped_column(
        SAEnum(
            ScenarioKind,
            values_callable=lambda enum: [m.value for m in enum],
            create_constraint=True,
            name="scenario_kind",
        ),
        nullable=False,
    )
    #: How the course would run, in prose that says of itself that it is a
    #: scenario. Never empty; see the CHECK.
    narrative: Mapped[str] = mapped_column(Text, nullable=False)
    likelihood: Mapped[ScenarioLikelihood] = mapped_column(
        SAEnum(
            ScenarioLikelihood,
            values_callable=lambda enum: [m.value for m in enum],
            create_constraint=True,
            name="scenario_likelihood",
        ),
        nullable=False,
    )
    #: What would have to be communicated, and to whom, if this course ran.
    #: May be empty where the stored lines support no sentence — an omission,
    #: never an invented Kommunikationsbedarf.
    communication_need: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )
    #: The standards this prose was written under, captured when the prompt was
    #: composed — the same terms as :attr:`StakeholderSelection.brain_version`.
    brain_version: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
    )

    triggers: Mapped[list["ScenarioTrigger"]] = relationship(
        back_populates="scenario", lazy="selectin", cascade="all, delete-orphan"
    )
    groups: Mapped[list["ScenarioStakeholder"]] = relationship(
        back_populates="scenario", lazy="selectin", cascade="all, delete-orphan"
    )


class ScenarioStakeholder(Base):
    """One group of the standing map this scenario would touch.

    A pointer into :class:`Stakeholder`, never a copy: a contact corrected on
    the map is corrected under every scenario at once, and a group the map does
    not hold is dropped in :mod:`newspulse.scenarios` rather than invented
    beside it.
    """

    __tablename__ = "scenario_stakeholders"
    __table_args__ = (
        UniqueConstraint(
            "scenario_id", "stakeholder_id", name="uq_scenario_stakeholders_once"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scenario_id: Mapped[int] = mapped_column(
        ForeignKey("scenarios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stakeholder_id: Mapped[int] = mapped_column(
        ForeignKey("stakeholders.id", ondelete="CASCADE"), nullable=False, index=True
    )

    scenario: Mapped["Scenario"] = relationship(back_populates="groups")
    stakeholder: Mapped["Stakeholder"] = relationship(lazy="selectin")


class ScenarioTrigger(Base):
    """One machine-checkable condition under which a scenario starts (DEC-5).

    ``fired_at`` is the whole latch, and it is a stored column rather than a
    set in memory for one reason the acceptance states outright: a trigger that
    has fired must not fire again, *not even after a restart*. Anything held in
    the process would re-announce every standing trigger on the next boot, and
    a channel that re-announces is a channel that stops being read.

    ``fired_note`` is what actually matched, and *only* that — the outlet, the
    headline, the journalist, the name. Which condition held is the
    ``condition`` column, and the page and the mail put its sentence beside the
    note, so the note carries no prose of this tool's own: a German sentence
    written into a stored value would stand untranslated on the English page,
    beside chrome that switched. The CHECK ties note and firing together: a
    firing that cannot say what it saw is a red mark on an issue nobody can
    act on.
    """

    __tablename__ = "scenario_triggers"
    __table_args__ = (
        # The same condition stands once per scenario: a second copy would fire
        # twice for one event, which is the one thing this table must not do.
        UniqueConstraint("scenario_id", "condition", name="uq_scenario_triggers_once"),
        CheckConstraint(
            "fired_at IS NULL OR fired_note <> ''", name="ck_scenario_triggers_note"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scenario_id: Mapped[int] = mapped_column(
        ForeignKey("scenarios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    condition: Mapped[TriggerCondition] = mapped_column(
        SAEnum(
            TriggerCondition,
            values_callable=lambda enum: [m.value for m in enum],
            create_constraint=True,
            name="trigger_condition",
        ),
        nullable=False,
    )
    #: When the condition was first found to hold. NULL means it has not, and
    #: it is written exactly once — the restart-proof half of "einmal gemeldet".
    fired_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    #: What matched, in one line. Never empty once fired; see the CHECK.
    fired_note: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )

    scenario: Mapped["Scenario"] = relationship(back_populates="triggers")

    @property
    def has_fired(self) -> bool:
        """Whether this condition has already been found to hold."""
        return self.fired_at is not None


class ResponseOption(Base):
    """One way of answering an issue, with what it buys and what it costs.

    ``no_response`` marks the option that is always on the list: "nicht
    reagieren", graded like every other. A tool that can only propose acting
    proposes acting, and the most expensive mistake in this trade is the
    statement that gives a matter the publicity it did not yet have.

    ``recommended`` marks the one option the answer puts forward, and the
    CHECK ties the speed to it: a recommendation that does not say how fast
    lets "schnell" and "sofort" mean the same thing, which is what the closed
    :class:`ResponseSpeed` set exists to prevent.
    """

    __tablename__ = "response_options"
    __table_args__ = (
        CheckConstraint("label <> ''", name="ck_response_options_label"),
        CheckConstraint("position >= 1", name="ck_response_options_position"),
        # The recommendation names a speed. Held here rather than only in
        # :mod:`newspulse.scenarios` so it survives every future writer.
        CheckConstraint(
            "recommended = 0 OR speed IS NOT NULL", name="ck_response_options_speed"
        ),
        UniqueConstraint("issue_id", "position", name="uq_response_options_position"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    issue_id: Mapped[int] = mapped_column(
        ForeignKey("issues.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The option in one line. Never empty; see the CHECK.
    label: Mapped[str] = mapped_column(String(RESPONSE_LABEL_MAX), nullable=False)
    #: What it buys, what it costs, and how far it could carry the matter. The
    #: three the acceptance names, and the reason "nicht reagieren" is a real
    #: option here rather than a caption: it is graded on the same three.
    benefit: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    risk: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    escalation: Mapped[EscalationPotential] = mapped_column(
        SAEnum(
            EscalationPotential,
            values_callable=lambda enum: [m.value for m in enum],
            create_constraint=True,
            name="escalation_potential",
        ),
        nullable=False,
        default=EscalationPotential.MITTEL,
        server_default=EscalationPotential.MITTEL.value,
    )
    #: Whether this is the "nicht reagieren" row. Exactly one per issue: no set
    #: is stored without it, and a second one is collapsed back to an ordinary
    #: option — both in :mod:`newspulse.scenarios`, where the answer is read.
    no_response: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    recommended: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    #: How fast to move, from the closed set. NULL on every option but the
    #: recommended one; see the CHECK.
    speed: Mapped[ResponseSpeed | None] = mapped_column(
        SAEnum(
            ResponseSpeed,
            values_callable=lambda enum: [m.value for m in enum],
            create_constraint=True,
            name="response_speed",
        ),
        nullable=True,
    )
    #: 1-based rank in the order the options are read.
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )
    brain_version: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
    )


# --- Das Entscheidungspapier (RIS-05) ----------------------------------------------


class PacketSection(StrEnum):
    """The parts a decision packet stands in, and the separation *is* the value.

    ``BELEGT`` is the only one that carries weight, and it carries it because
    every sentence under it resolves to a stored row. ``UNBESTAETIGT`` is where
    a sentence lands that does not — said out loud rather than dropped, because
    "we have heard this and cannot stand it up" is itself worth reading in the
    hour a decision is made. ``OFFEN`` is the questions nobody has answered yet.

    The fourth part, the contradictions, is :class:`DecisionContradiction` and
    not a member here: it is not a sentence with a section, it is two named
    sides and what stands between them, and a schema that let it be one row of
    prose would let a contradiction be reported with only one side.
    """

    BELEGT = "belegt"
    UNBESTAETIGT = "unbestätigt"
    OFFEN = "offen"


class EvidenceKind(StrEnum):
    """The kinds of stored row a sentence of the packet may resolve to.

    A closed set, and every member is a table in this schema: a piece of
    coverage, its analysis, a profile field, a market signal, a reply in the
    connected mailbox, and a text of our own that a person released. A
    "Kennung" outside this set resolves to nothing, and the sentence that
    carried it is not led as belegt — which is the whole safety argument of the
    packet.
    """

    BEITRAG = "beitrag"
    ANALYSE = "analyse"
    PROFIL = "profil"
    MARKTSIGNAL = "marktsignal"
    MAIL = "mail"
    TEXT = "text"


class SourceRank(StrEnum):
    """Die Quellenordnung: what outweighs what, in declaration order.

    Fixed, and printed on the paper rather than kept in a module nobody opens:
    a reader deciding under pressure has to be able to see that the sentence
    they are about to act on rests on a confirmed internal statement and not on
    a journalist's question in an inbox. The order here *is* the order — a
    member's position in this enum is what :mod:`newspulse.decision` ranks by,
    so re-ordering the four is a deliberate edit and never a side effect.
    """

    INTERN = "bestätigte interne Angabe"
    BEHOERDE = "Behörde oder Originaldokument"
    MEDIEN = "verifizierter Medienbericht"
    UEBRIGES = "alles Übrige"


class GapKind(StrEnum):
    """The named gaps a packet reports, from a closed set.

    A gap is the part a person under pressure does not assemble for themselves,
    so it has to be a *named* line with a link to where it is closed and never a
    blank space. Closed, because a gap the tool invented would send somebody
    looking for a field that does not exist.

    Three of them are found in the stored material when the paper is built and
    are frozen onto it (:class:`DecisionGap`). The two that are properties of
    the paper itself — the decider and the deadline — are read live off
    :class:`DecisionPacket`, because naming them is what gets them filled in,
    and a frozen row would keep saying they are missing after somebody had
    supplied them.
    """

    SPRECHER = "sprecher"
    KRISENKONTAKT = "krisenkontakt"
    BETROFFENENZAHL = "betroffenenzahl"
    ENTSCHEIDER = "entscheider"
    FRIST = "frist"


#: How much of a decider's name the paper keeps. The same ceiling
#: :data:`CRISIS_DECLARED_BY_MAX` sets, and for the same reason: a long sign-in
#: name costs its tail, never the record of who decided.
DECISION_NAME_MAX = CRISIS_DECLARED_BY_MAX

#: How much of one evidence line's label is kept on the paper. A copy rather
#: than a pointer (see :class:`DecisionEvidence`), so the width is this table's
#: business and not the archive's.
EVIDENCE_LABEL_MAX = 300


class DecisionPacket(Base):
    """One decision paper: what is known, what is not, and who decides by when.

    Written on a button press to an issue or to a crisis, and **never replaced**:
    "ein neues Papier zum selben Issue ersetzt das alte nicht, sondern tritt
    daneben" is the acceptance, so there is deliberately no UNIQUE over the
    anchor and no upsert anywhere in :mod:`newspulse.decision`. Two papers a week
    apart are the record of how the reading changed, which is exactly the
    question asked afterwards.

    The paper is stored **as it read**: every statement, every piece of evidence
    and every contradiction is a row with its text copied rather than a pointer
    resolved at render time. That is the opposite of what a *draft* report does
    and the same thing a released one does, for the same reason — a piece of
    coverage dismissed in October must not silently change what the September
    paper says it said. The ids are kept beside the copies, so a reader can
    still walk back to the row a sentence came from.

    ``decision``/``decided_by``/``decided_at`` are the three the CHECK ties
    together: afterwards the question is always what was decided, by whom, and
    on what the decision rested, and a decision recorded without a name answers
    two thirds of it.
    """

    __tablename__ = "decision_packets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The issue this paper was written to. CASCADE: a paper about an issue that
    #: no longer exists explains nothing.
    issue_id: Mapped[int | None] = mapped_column(
        ForeignKey("issues.id", ondelete="CASCADE"), nullable=True, index=True
    )
    crisis_id: Mapped[int | None] = mapped_column(
        ForeignKey("crises.id", ondelete="CASCADE"), nullable=True, index=True
    )
    #: What happened, in the packet's own words. Never empty: a paper that
    #: cannot say what this is about is not stored at all.
    situation: Mapped[str] = mapped_column(Text, nullable=False)
    #: What is to be decided now. May be empty where the material supported no
    #: sentence — an omission the paper names, never an invented question.
    question: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    #: Who decides. Empty is a *named gap at the top of the paper*, never a
    #: blank line, and it is set by a person: a decider this tool nominated
    #: would be a name nobody agreed to.
    decision_maker: Mapped[str] = mapped_column(
        String(DECISION_NAME_MAX), nullable=False, default="", server_default=""
    )
    #: By when. NULL is the other named gap at the top.
    deadline: Mapped[dt.datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )
    #: Who pressed the button: a user name where the tool has one, the
    #: ``"mensch"`` token otherwise — the discipline every other row here keeps.
    created_by: Mapped[str] = mapped_column(String(DECISION_NAME_MAX), nullable=False)

    #: The decision that was taken, in the deciding person's own words. Empty
    #: until one is recorded, and from then on the paper is closed.
    decision: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    decided_by: Mapped[str] = mapped_column(
        String(DECISION_NAME_MAX), nullable=False, default="", server_default=""
    )
    decided_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    #: The standards the prose on this paper was written under, captured when
    #: the prompt was composed — the same terms as :attr:`Scenario.brain_version`.
    brain_version: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
    )

    statements: Mapped[list["DecisionStatement"]] = relationship(
        back_populates="packet",
        lazy="selectin",
        cascade="all, delete-orphan",
        order_by="DecisionStatement.position",
    )
    contradictions: Mapped[list["DecisionContradiction"]] = relationship(
        back_populates="packet",
        lazy="selectin",
        cascade="all, delete-orphan",
        order_by="DecisionContradiction.position",
    )
    stored_gaps: Mapped[list["DecisionGap"]] = relationship(
        back_populates="packet",
        lazy="selectin",
        cascade="all, delete-orphan",
        order_by="DecisionGap.position",
    )

    __table_args__ = (
        # A paper hangs on exactly one occasion — an issue or a crisis. The same
        # spelling ``stakeholder_selections`` uses, so the two read alike.
        CheckConstraint(
            "(issue_id IS NULL) <> (crisis_id IS NULL)",
            name="ck_decision_packets_one_anchor",
        ),
        CheckConstraint("situation <> ''", name="ck_decision_packets_situation"),
        # A recorded decision always carries its words and the person. Held at
        # the schema because "what was decided, and by whom" is the question the
        # paper exists to answer months later, and a future writer that filled
        # only one of the three would leave it unanswerable.
        CheckConstraint(
            "decided_at IS NULL OR (decision <> '' AND decided_by <> '')",
            name="ck_decision_packets_decision",
        ),
    )

    @property
    def is_decided(self) -> bool:
        """Whether a decision has been recorded. The paper is closed from then on."""
        return self.decided_at is not None


class DecisionStatement(Base):
    """One sentence of the paper, in the part it belongs to.

    ``source_rank`` is the Quellenordnung made visible: a ``BELEGT`` sentence
    carries the rank of the strongest line under it, and no other section
    carries one at all. The CHECK holds both halves of that, so a future writer
    cannot file an unconfirmed sentence under "bestätigte interne Angabe", nor
    lead a sentence as belegt without saying where it sits in the order.

    A ``BELEGT`` row without evidence is impossible by construction:
    :mod:`newspulse.decision` moves a sentence whose Kennung resolves to nothing
    into ``UNBESTAETIGT`` before anything is written. The rank column is what
    makes that visible at the schema — no rank, no claim.
    """

    __tablename__ = "decision_statements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    packet_id: Mapped[int] = mapped_column(
        ForeignKey("decision_packets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    section: Mapped[PacketSection] = mapped_column(
        SAEnum(
            PacketSection,
            values_callable=lambda enum: [m.value for m in enum],
            create_constraint=True,
            name="packet_section",
        ),
        nullable=False,
    )
    #: The sentence as it is read. Never empty; see the CHECK.
    text: Mapped[str] = mapped_column(Text, nullable=False)
    #: Where the strongest line under this sentence sits in the Quellenordnung.
    #: NULL on every section but ``belegt``; see the CHECK.
    source_rank: Mapped[SourceRank | None] = mapped_column(
        SAEnum(
            SourceRank,
            values_callable=lambda enum: [m.value for m in enum],
            create_constraint=True,
            name="source_rank",
        ),
        nullable=True,
    )
    #: 1-based rank across the whole paper, so the order the sentences were
    #: written in survives a re-read.
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    packet: Mapped["DecisionPacket"] = relationship(back_populates="statements")
    evidence: Mapped[list["DecisionEvidence"]] = relationship(
        back_populates="statement",
        lazy="selectin",
        cascade="all, delete-orphan",
        order_by="DecisionEvidence.id",
    )

    __table_args__ = (
        CheckConstraint("text <> ''", name="ck_decision_statements_text"),
        CheckConstraint("position >= 1", name="ck_decision_statements_position"),
        # Belegt and only belegt carries a rank. Written as the two-sided
        # equivalence it is, because either half alone would let the paper lie:
        # a rank on an unconfirmed sentence claims an authority nobody gave it,
        # and a belegt sentence without one hides where it sits in the order.
        CheckConstraint(
            "(section = 'belegt') = (source_rank IS NOT NULL)",
            name="ck_decision_statements_rank",
        ),
        UniqueConstraint(
            "packet_id", "position", name="uq_decision_statements_position"
        ),
    )


class DecisionEvidence(Base):
    """The stored row one belegt sentence resolves to — id kept, text copied.

    ``kind`` and ``ref_id`` together are the "Kennung der Zeile" the acceptance
    asks every belegt sentence to carry, and they are what a reader walks back
    along. ``label``, ``source``, ``happened_at`` and ``url`` are copies of what
    that row said *at the time*, because the paper is the record of what a
    decision rested on: a headline re-titled or a piece of coverage dismissed
    afterwards must not be able to change it.

    Deliberately no foreign key to the six tables it can point into. A real FK
    would need six nullable columns and would delete evidence out from under a
    stored paper on a CASCADE — which is the one thing this table exists to
    prevent.
    """

    __tablename__ = "decision_evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    statement_id: Mapped[int] = mapped_column(
        ForeignKey("decision_statements.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[EvidenceKind] = mapped_column(
        SAEnum(
            EvidenceKind,
            values_callable=lambda enum: [m.value for m in enum],
            create_constraint=True,
            name="evidence_kind",
        ),
        nullable=False,
    )
    #: The stored row's own id. Half of the Kennung, and never resolved on
    #: render — see the class docstring.
    ref_id: Mapped[int] = mapped_column(Integer, nullable=False)
    #: What the row said: the headline, the profile field and its value, the
    #: subject line. Never empty; see the CHECK.
    label: Mapped[str] = mapped_column(String(EVIDENCE_LABEL_MAX), nullable=False)
    #: Where it came from: the outlet, the publisher, the sender.
    source: Mapped[str] = mapped_column(
        String(EVIDENCE_LABEL_MAX), nullable=False, default="", server_default=""
    )
    happened_at: Mapped[dt.datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )
    #: The row's own external address, where it has one. Never a link back into
    #: this application: the downloaded paper carries none of those.
    url: Mapped[str] = mapped_column(
        String(2048), nullable=False, default="", server_default=""
    )

    statement: Mapped["DecisionStatement"] = relationship(back_populates="evidence")

    __table_args__ = (
        CheckConstraint("label <> ''", name="ck_decision_evidence_label"),
        # One line stands under one sentence once: a doubled Kennung is not two
        # pieces of evidence, it is the same one counted twice.
        UniqueConstraint(
            "statement_id", "kind", "ref_id", name="uq_decision_evidence_once"
        ),
    )


class DecisionContradiction(Base):
    """One contradiction, with **both** sides named as stored rows.

    The columns are doubled on purpose, and the NOT NULLs are the acceptance
    itself: "ein Widerspruch mit nur einer Seite wird nicht gemeldet". A
    reported contradiction that cannot name what it contradicts is worse than
    no reported contradiction, because in a crisis it is believed — so a schema
    that allowed one side to be absent would be the defect, not the code that
    happened to fill both.

    Both sides copy their line the way :class:`DecisionEvidence` does, and for
    the same reason.
    """

    __tablename__ = "decision_contradictions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    packet_id: Mapped[int] = mapped_column(
        ForeignKey("decision_packets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: What the contradiction consists of, in one sentence. Never empty.
    note: Mapped[str] = mapped_column(Text, nullable=False)

    left_kind: Mapped[EvidenceKind] = mapped_column(
        SAEnum(
            EvidenceKind,
            values_callable=lambda enum: [m.value for m in enum],
            create_constraint=True,
            # A name of its own rather than ``evidence_kind``: the CHECK an
            # Enum emits is named after it, and two constraints of one name on
            # one table is a schema nobody can alter later.
            name="contradiction_left_kind",
        ),
        nullable=False,
    )
    left_ref_id: Mapped[int] = mapped_column(Integer, nullable=False)
    left_label: Mapped[str] = mapped_column(String(EVIDENCE_LABEL_MAX), nullable=False)
    left_source: Mapped[str] = mapped_column(
        String(EVIDENCE_LABEL_MAX), nullable=False, default="", server_default=""
    )

    right_kind: Mapped[EvidenceKind] = mapped_column(
        SAEnum(
            EvidenceKind,
            values_callable=lambda enum: [m.value for m in enum],
            create_constraint=True,
            name="contradiction_right_kind",
        ),
        nullable=False,
    )
    right_ref_id: Mapped[int] = mapped_column(Integer, nullable=False)
    right_label: Mapped[str] = mapped_column(String(EVIDENCE_LABEL_MAX), nullable=False)
    right_source: Mapped[str] = mapped_column(
        String(EVIDENCE_LABEL_MAX), nullable=False, default="", server_default=""
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    packet: Mapped["DecisionPacket"] = relationship(back_populates="contradictions")

    __table_args__ = (
        CheckConstraint("note <> ''", name="ck_decision_contradictions_note"),
        CheckConstraint(
            "left_label <> '' AND right_label <> ''",
            name="ck_decision_contradictions_sides",
        ),
        CheckConstraint("position >= 1", name="ck_decision_contradictions_position"),
        UniqueConstraint(
            "packet_id", "position", name="uq_decision_contradictions_position"
        ),
    )


class DecisionGap(Base):
    """One named gap the paper found in the stored material, frozen onto it.

    Only the three gaps that are statements about the *material* live here —
    the missing spokesperson, the missing crisis contact, the absence of any
    confirmed internal figure. They are frozen because the paper is the record
    of what was known at the time, and a profile filled in on Thursday must not
    quietly remove Monday's gap from Monday's paper.

    The decider and the deadline are deliberately *not* stored: they are
    columns on :class:`DecisionPacket`, they are filled from the paper itself,
    and a frozen row would keep reporting them missing after somebody supplied
    them. :func:`newspulse.decision.gaps` puts both origins into one list.
    """

    __tablename__ = "decision_gaps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    packet_id: Mapped[int] = mapped_column(
        ForeignKey("decision_packets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[GapKind] = mapped_column(
        SAEnum(
            GapKind,
            values_callable=lambda enum: [m.value for m in enum],
            create_constraint=True,
            name="gap_kind",
        ),
        nullable=False,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    packet: Mapped["DecisionPacket"] = relationship(back_populates="stored_gaps")

    __table_args__ = (
        # The same gap is named once per paper: a second copy is not a second
        # gap, it is the same one printed twice.
        UniqueConstraint("packet_id", "kind", name="uq_decision_gaps_once"),
        CheckConstraint("position >= 1", name="ck_decision_gaps_position"),
    )


__all__ = [
    "DECISION_NAME_MAX",
    "EVIDENCE_LABEL_MAX",
    "DecisionContradiction",
    "DecisionEvidence",
    "DecisionGap",
    "DecisionPacket",
    "DecisionStatement",
    "EvidenceKind",
    "GapKind",
    "PacketSection",
    "SourceRank",
    "Scenario",
    "ScenarioKind",
    "ScenarioLikelihood",
    "ScenarioStakeholder",
    "ScenarioTrigger",
    "TriggerCondition",
    "ResponseOption",
    "ResponseSpeed",
    "EscalationPotential",
    "RESPONSE_LABEL_MAX",
    "RESPONSE_OPTIONS_MIN",
    "Crisis",
    "ISSUE_SCALE_MAX",
    "ISSUE_SCALE_MIN",
    "Issue",
    "STAKEHOLDER_TEXT_MAX",
    "Stakeholder",
    "StakeholderLevel",
    "StakeholderSelection",
    "IssueDismissal",
    "IssueSignal",
    "IssueStatus",
    "ReputationReading",
    "ReputationState",
    "NewsjackOpportunity",
    "Standing",

    "CrisisDismissal",
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
