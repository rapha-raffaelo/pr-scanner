"""Studies, regulation and events: three classes a news feed cannot carry.

The market radar reads news, which means it sees a market only after the market
has spoken. A study that would back a client's thesis for six months, a
consultation that closes in five weeks, a conference whose call for speakers
shuts on Friday — none of it is a news item, and all of it is where a PR
consultant's lead time comes from.

Each class breaks the shape of a news item in a different place, and this module
is where that difference is read out of an otherwise ordinary RSS entry:

* a **study** has already been published, so its publication date *is* its
  actionable date and it needs no other;
* **regulation** is dated in the future — "tritt am 1. Januar 2027 in Kraft" —
  and the lead time is the whole value, so that date is parsed out of the text and
  stored as it stands;
* an **event** is a date and a stage, and it is the only class with a deadline
  attached, because a call for speakers closes.

Where they come from is DEC-1 B: a curated list that applies to every mandate
(``market_sources.toml``, data rather than code) plus a per-mandate search in the
client's own field, using the industry term the tool already measures. The search
half will return things that are not really studies, so every signal records which
half produced it and the page can be judged accordingly.

Nothing here decides anything. It fetches, parses, and refuses to store what the
mandate already has — the sweep in :mod:`newspulse.job` owns the fault boundary,
one guard per class, so a dead regulatory feed never costs the other two.
"""

from __future__ import annotations

import datetime as dt
import functools
import logging
import re
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, gnews
from .feeds import read_registry
from .ingest import FeedItem, fetch_feed
from .matching import canonical_url, dedup_title_hash
from .models import (
    Analysis,
    Article,
    Client,
    MarketSignal,
    SignalKind,
    SignalOrigin,
    TopicHit,
)

_log = logging.getLogger(__name__)

# The curated list, resolved as package data so it is found whether NewsPulse runs
# from a source checkout or an installed wheel.
_SOURCES_FILENAME = "market_sources.toml"

# Top-level key in the TOML holding the array of source tables.
_SOURCES_KEY = "sources"

# How a search-built source is labelled, so a log line and the stored publisher
# both say the item came from the field search rather than from a curated feed.
_SEARCH_LABEL = "Feldsuche {kind}: {client}"

# A callable shaped like :func:`newspulse.ingest.fetch_feed`. Injected so the
# fetchers can be driven over fixture payloads with no network anywhere near them.
FetchFeed = Callable[..., list[FeedItem]]


@dataclass(frozen=True, slots=True)
class MarketSource:
    """One place a class of signal is fetched from.

    Deliberately not :class:`newspulse.feeds.Feed`. A feed is a publication whose
    items are news; a source here is a publication *of a class*, and the two
    fields that differ — which class it carries, and whether it is curated or
    searched — are exactly the two a signal has to be stored with.
    """

    name: str
    url: str
    kind: SignalKind
    origin: SignalOrigin = SignalOrigin.KURATIERT
    # True for an aggregator feed whose entries each name their own publisher
    # (the field search); false for an institute's own feed, where this name *is*
    # the publisher.
    per_entry_source: bool = False


@dataclass(frozen=True, slots=True)
class SignalDraft:
    """One parsed signal, before anything has decided whether to store it.

    Split from :class:`~newspulse.models.MarketSignal` so parsing can be tested
    without a database, and so deduplication has something to reject that was
    never written.
    """

    kind: SignalKind
    title: str
    publisher: str
    url: str
    origin: SignalOrigin
    summary: str = ""
    published_at: dt.datetime | None = None
    effective_at: dt.datetime | None = None
    deadline_at: dt.datetime | None = None


# --- The curated list, as data -------------------------------------------------


def _parse_sources(data: str, *, origin: str) -> list[MarketSource]:
    """Parse TOML text into sources, dropping malformed entries rather than the file.

    A row missing a field, or naming a class that does not exist, is logged and
    skipped: one typo must never take the other eleven sources down with it.
    """
    sources: list[MarketSource] = []
    for index, entry in enumerate(tomllib.loads(data).get(_SOURCES_KEY, [])):
        name, url, raw_kind = entry.get("name"), entry.get("url"), entry.get("kind")
        if not name or not url or not raw_kind:
            _log.warning(
                "Skipping market source #%d in %s: missing name, url or kind (%r)",
                index, origin, entry,
            )
            continue
        try:
            kind = SignalKind(str(raw_kind).strip().casefold())
        except ValueError:
            _log.warning(
                "Skipping market source %r in %s: %r is not a signal class",
                name, origin, raw_kind,
            )
            continue
        sources.append(MarketSource(name=name, url=url, kind=kind))
    return sources


def load_sources(path: str | Path | None = None) -> list[MarketSource]:
    """Load the curated source list; with no ``path``, the packaged one."""
    data, origin = read_registry(_SOURCES_FILENAME, path)
    return _parse_sources(data, origin=origin)


# --- Reading a German date out of a syndicated line -----------------------------

_MONTHS = {
    "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5,
    "juni": 6, "juli": 7, "august": 8, "september": 9, "oktober": 10,
    "november": 11, "dezember": 12,
}

# Three spellings, because official German sources use all three in the same
# sentence: "01.01.2027", "1. Januar 2027", and the ISO form in EU material.
_NUMERIC_DATE_RE = re.compile(r"\b(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{4})\b")
_WRITTEN_DATE_RE = re.compile(
    r"\b(\d{1,2})\.\s?(" + "|".join(_MONTHS) + r")\s+(\d{4})\b", re.IGNORECASE
)
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")

# How far past a cue phrase a date still belongs to it. Roughly one German clause;
# beyond that the date in the sentence is about something else, and a regulatory
# calendar that guesses is worse than one that admits it found no date.
_CUE_WINDOW = 80

# What an item says right before the date it lands or opens on.
_EFFECTIVE_CUES = (
    "in kraft", "tritt", "gilt ab", "gelten ab", "anwendbar ab", "wirksam ab",
    "startet am", "beginnt am", "beginnt ab", "ab dem",
)

# ... and right before the date the door closes. Kept apart from the cues above
# because "you may still comment until" and "it now applies to you" are opposite
# instructions to a consultant, and one date column could only tell one of them.
_DEADLINE_CUES = (
    "frist", "bis zum", "endet am", "läuft bis", "stellungnahme", "konsultation",
    "einsendeschluss", "anmeldeschluss", "einreichung", "bewerbungsschluss",
)

# An event names its own date the way a programme does.
_EVENT_CUES = ("findet am", "findet vom", "am ", "vom ", "termin")

# The cues that are whole German words rather than compound stems, and so must not
# match inside one: without this "am " fires on the last two letters of "Team ",
# and the event's date is then anchored to a word that says nothing about it. The
# stems are deliberately absent — "frist" has to keep matching "Anmeldefrist".
_WHOLE_WORD_CUES = frozenset({"am ", "vom "})

# A call for speakers is the one deadline an event carries, and it is not the
# registration deadline: a consultant needs to know when he can still get on
# stage, not when the ticket price rises.
_SPEAKER_CUES = (
    "call for papers", "call for speakers", "referenten", "vortragende",
    "beitrag einreichen", "einreichungsfrist", "programmvorschläge",
) + _DEADLINE_CUES


def _dates(text: str) -> list[tuple[int, dt.datetime]]:
    """Every date in ``text`` as ``(position, UTC midnight)``, in reading order.

    Midnight rather than a time, because none of these sources publish one: a law
    takes effect on a day. An impossible date (``31.02.2027``) is dropped rather
    than raised on — a typo in one line must not cost the whole item.
    """
    found: list[tuple[int, dt.datetime]] = []
    for match in _NUMERIC_DATE_RE.finditer(text):
        day, month, year = (int(g) for g in match.groups())
        found.append((match.start(), _at_midnight(year, month, day)))
    for match in _WRITTEN_DATE_RE.finditer(text):
        day, month_name, year = match.groups()
        found.append(
            (match.start(), _at_midnight(int(year), _MONTHS[month_name.casefold()], int(day)))
        )
    for match in _ISO_DATE_RE.finditer(text):
        year, month, day = (int(g) for g in match.groups())
        found.append((match.start(), _at_midnight(year, month, day)))
    return sorted((pos, when) for pos, when in found if when is not None)


def _at_midnight(year: int, month: int, day: int) -> dt.datetime | None:
    """That calendar day at UTC midnight, or ``None`` when it is not a real day."""
    try:
        return dt.datetime(year, month, day, tzinfo=dt.UTC)
    except ValueError:
        return None


@functools.lru_cache(maxsize=None)
def _cue_pattern(cue: str) -> re.Pattern[str]:
    """One cue as a pattern matched against the *original* text.

    Case-insensitively rather than over a casefolded copy, because
    :meth:`str.casefold` is not length-preserving in German — it expands "ß" to
    "ss" — and a cue's offset then no longer lines up with the offsets
    :func:`_dates` found in the text it is compared against. One "ß" before the
    cue was enough to push the following date out of :data:`_CUE_WINDOW`, and
    since ``deadline_at`` has no positional fallback, an ordinary sentence about a
    "Maßnahme" or an "Einsendeschluß" silently lost its cut-off date.

    "ss" still matches "ß" so the pre-1996 spellings official sources keep in
    their archives ("Einsendeschluß") are read — but as an alternation inside the
    pattern, which does not move a single character.
    """
    body = re.escape(cue).replace("ss", "(?:ss|ß)")
    boundary = r"(?<!\w)" if cue in _WHOLE_WORD_CUES else ""
    return re.compile(boundary + body, re.IGNORECASE)


def _mentions(text: str, cues: Sequence[str]) -> bool:
    """Whether ``text`` uses any of ``cues`` at all, dated or not."""
    return any(_cue_pattern(cue).search(text) for cue in cues)


def _dated(text: str, cues: Sequence[str]) -> dt.datetime | None:
    """The first date that follows one of ``cues`` inside :data:`_CUE_WINDOW`.

    Cue-anchored rather than positional, because an item routinely carries both
    of the dates this module distinguishes, in either order: "Die Konsultation
    endet am 30.09.2026, die Verordnung tritt am 01.01.2027 in Kraft."
    """
    dates = _dates(text)
    if not dates:
        return None
    best: tuple[int, dt.datetime] | None = None
    for cue in cues:
        for match in _cue_pattern(cue).finditer(text):
            end = match.end()
            for pos, when in dates:
                if end <= pos <= end + _CUE_WINDOW:
                    if best is None or pos < best[0]:
                        best = (pos, when)
                    break
    return best[1] if best is not None else None


def _first_from(
    text: str, reference: dt.datetime, *, skip: dt.datetime | None = None
) -> dt.datetime | None:
    """The first date in ``text`` that has not already passed, ignoring ``skip``.

    The fallback for an item that states its date bare — official calendars often
    do — where no cue phrase vouches for what the date means. Past dates are
    ignored because in a forward calendar they are almost always a reference to
    the predecessor rule rather than the date this item lands on.
    """
    for _pos, when in _dates(text):
        if when < reference or when == skip:
            continue
        return when
    return None


def _positional(
    text: str,
    reference: dt.datetime,
    *,
    deadline: dt.datetime | None,
    deadline_cues: Sequence[str],
) -> dt.datetime | None:
    """:func:`_first_from`, unless the one date in the text is plainly a deadline.

    The fallback reads an uncued date as the date the item lands on. That is right
    for a calendar entry stated bare, and wrong in exactly one case: the item talks
    about a closing door but states the date *before* the cue — "Bis 30.09.2026
    können Stellungnahmen eingereicht werden" — where :func:`_dated` looks only
    forward and finds nothing to anchor. Filing that date as ``effective_at``
    tells the consultant "it now applies to you" about a consultation he can still
    answer, which is the one confusion this module exists to prevent. So when a
    deadline cue is present and none of the dates could be tied to it, the item is
    reported as having no effective date rather than a misread one.
    """
    if deadline is None and _mentions(text, deadline_cues):
        return None
    return _first_from(text, reference, skip=deadline)


# --- One fetcher per class, behind one interface --------------------------------


class MarketFetcher:
    """One class of market signal: where it comes from, and which dates it carries.

    Subclasses differ in exactly two things — the class they produce, and which of
    the dates an item of that class actually has. Everything else is shared: the
    curated sources, the per-mandate field search on top of them, the fetch, and
    the mapping onto a draft. That is what "three fetchers, one shape" means.

    Exceptions are not caught here. A source that cannot be reached has to reach
    the sweep's per-class guard, which is the only place that knows the other two
    classes exist and can log at ERROR without stopping them.

    One instance per sweep, not per mandate: the curated list is the same list for
    every client, and the fetch is cached on the instance so it is issued once for
    the whole portfolio. See :meth:`_items`.
    """

    kind: ClassVar[SignalKind]
    #: The words a German item of this class uses about itself. Used only to build
    #: the field search; the curated sources need no query at all.
    query_terms: ClassVar[tuple[str, ...]] = ()

    def __init__(
        self,
        *,
        fetch: FetchFeed = fetch_feed,
        sources: Sequence[MarketSource] | None = None,
    ) -> None:
        self._fetch = fetch
        self._sources = (
            list(sources)
            if sources is not None
            else [s for s in load_sources() if s.kind is self.kind]
        )
        # What each source answered this sweep, by (url, label): the items, or the
        # exception it raised. Lives as long as the instance, which is one sweep.
        self._answered: dict[tuple[str, str], list[FeedItem] | Exception] = {}

    def sources_for(self, client: Client) -> list[MarketSource]:
        """The curated sources for this class, plus this mandate's field search."""
        search = self._search_source(client)
        return [*self._sources, *([search] if search is not None else [])]

    def _search_source(self, client: Client) -> MarketSource | None:
        """The per-mandate half of DEC-1 B, or ``None`` when it cannot be built.

        ``None`` when the mandate has no usable field. A class query without one is
        a bare ``"Studie" OR "Report"``, which returns the whole German press and
        calls it this client's market — worse than the honest gap the market page
        explains instead.
        """
        field = gnews.context_terms(client)
        if not field or not self.query_terms or not config.GOOGLE_NEWS_ENABLED:
            return None
        lang, country = gnews.edition_for(client)
        return MarketSource(
            name=_SEARCH_LABEL.format(kind=self.kind.value, client=client.name),
            url=gnews.query_url(
                list(self.query_terms), lang=lang, country=country, context=field
            ),
            kind=self.kind,
            origin=SignalOrigin.SUCHE,
            per_entry_source=True,
        )

    def collect(
        self, client: Client, *, since: dt.datetime, now: dt.datetime
    ) -> list[SignalDraft]:
        """Fetch every source for this class and parse each item into a draft."""
        drafts: list[SignalDraft] = []
        for source in self.sources_for(client):
            items = self._items(source, since=since, now=now)
            drafts.extend(
                self._draft(item, source, now)
                for item in items
                if item.title.strip() and item.link.strip()
            )
        return drafts

    def _items(
        self, source: MarketSource, *, since: dt.datetime, now: dt.datetime
    ) -> list[FeedItem]:
        """What one source answered, fetched at most once per sweep.

        ``collect`` runs per mandate, but eleven of the twelve curated sources are
        the *same* eleven for every mandate: destatis publishes one list of studies,
        not one per client of this agency. Fetching per mandate multiplied the
        curated list by the size of the portfolio — a ten-mandate portfolio asking
        the same authority for the same feed ten times every morning, which is how
        a well-behaved reader turns into something a 403 is written for. Only the
        field search legitimately differs per client, and it differs in the URL, so
        it caches to its own entry.

        Failures are cached too, and re-raised. The class is still reported as dark
        for every mandate — nothing about the fault boundary changes — but a source
        that is down is asked once rather than once per client.
        """
        key = (source.url, source.name)
        if key not in self._answered:
            try:
                self._answered[key] = self._fetch(
                    source.url,
                    since,
                    source=source.name,
                    fetched_at=now,
                    per_entry_source=source.per_entry_source,
                    # The whole reason this class can be reported as failed: see
                    # ingest.fetch_feed.
                    strict=True,
                )
            except Exception as exc:  # noqa: BLE001 — cached, then re-raised as-is
                self._answered[key] = exc
                raise
        answer = self._answered[key]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def _draft(
        self, item: FeedItem, source: MarketSource, now: dt.datetime
    ) -> SignalDraft:
        text = " ".join(part for part in (item.title, item.summary or "") if part)
        effective, deadline = self.read_dates(text, now)
        return SignalDraft(
            kind=self.kind,
            title=item.title.strip(),
            publisher=self._publisher(item, source),
            url=item.link.strip(),
            origin=source.origin,
            summary=item.summary or "",
            published_at=item.published_at,
            effective_at=effective,
            deadline_at=deadline,
        )

    @staticmethod
    def _publisher(item: FeedItem, source: MarketSource) -> str:
        """Who to credit: the institute, the authority, the organiser.

        For a curated feed that is the source itself — the feed *is* the publisher.
        For the field search it is whatever the entry named, and only that:
        :func:`newspulse.ingest._entry_source` falls back to the feed's own label
        when an aggregator entry carries no ``<source>``, and that label is the
        search ("Feldsuche studie: Arrakis Finance"), which would put the mandate's
        own name in the publisher column of a study it did not write. The column
        documents "" as publisher-unknown, and unknown is what this is.
        """
        named = (item.source or "").strip()
        if source.per_entry_source:
            return "" if named == source.name.strip() else named
        return named or source.name.strip()

    def read_dates(
        self, text: str, now: dt.datetime
    ) -> tuple[dt.datetime | None, dt.datetime | None]:
        """``(effective_at, deadline_at)`` for one item of this class."""
        return None, None


class StudyFetcher(MarketFetcher):
    """Studies: the one class whose actionable date is the date it was published.

    It carries neither of the other two dates, and that is deliberate rather than
    unfinished. A study does not land in the future and no door closes on it — its
    value is that it can be cited for months. Giving it a fabricated
    ``effective_at`` would put it in a forward calendar it does not belong in, and
    the market page would rank a six-month-old paper by a date nobody set.
    """

    kind = SignalKind.STUDIE
    query_terms = ("Studie", "Report", "Umfrage")


class RegulationFetcher(MarketFetcher):
    """Regulation: dated in the future, which is the entire point.

    Both dates are read, because an item routinely has both and they mean opposite
    things: the consultation closes on one, the rule applies from the other.
    """

    kind = SignalKind.REGULIERUNG
    query_terms = ("Gesetz", "Verordnung", "Konsultation")

    def read_dates(self, text, now):
        deadline = _dated(text, _DEADLINE_CUES)
        effective = _dated(text, _EFFECTIVE_CUES) or _positional(
            text, now, deadline=deadline, deadline_cues=_DEADLINE_CUES
        )
        return effective, deadline


class EventFetcher(MarketFetcher):
    """Events: a date and a stage, and the only class with a speaker deadline."""

    kind = SignalKind.VERANSTALTUNG
    query_terms = ("Konferenz", "Kongress", "Fachtagung")

    def read_dates(self, text, now):
        deadline = _dated(text, _SPEAKER_CUES)
        effective = _dated(text, _EVENT_CUES) or _positional(
            text, now, deadline=deadline, deadline_cues=_SPEAKER_CUES
        )
        return effective, deadline


def fetchers(*, fetch: FetchFeed = fetch_feed) -> list[MarketFetcher]:
    """The three fetchers, curated sources loaded once for all of them.

    One read of the TOML rather than three: the sweep builds these per run, and a
    per-class read would parse the same file once for every mandate in the
    portfolio.
    """
    curated = load_sources()
    return [
        cls(fetch=fetch, sources=[s for s in curated if s.kind is cls.kind])
        for cls in (StudyFetcher, RegulationFetcher, EventFetcher)
    ]


# --- Deduplication and storage --------------------------------------------------

# How far back a mandate's own news is compared against a new signal. A year,
# because a study a trade publication covered last spring can still arrive from a
# curated list this morning, while an archive older than that cannot plausibly be
# the same document as an item on a forward calendar — and the read happens once
# per mandate per sweep, so unbounded it grows with the archive forever.
_COVERAGE_LOOKBACK = dt.timedelta(days=365)


@dataclass(slots=True)
class Seen:
    """What one mandate already has, by URL identity and by normalized title.

    Mutable and carried across the three classes of one sweep on purpose: the URL
    uniqueness is per client, not per class, so a study and an event arriving from
    the same page would otherwise collide at the insert rather than at the check.

    The two title sets differ in exactly the way the two identities do:

    * ``titles`` holds ``(class, hash)``, because the stored uniqueness is
      ``(client, kind, title_hash)`` — a conference and the study it presents share
      a headline legitimately, and a flat set would drop whichever of them arrived
      second, before it ever reached the constraint that allows it;
    * ``article_titles`` holds bare hashes, because a headline already in the
      mandate's own news is the same document whichever class would carry it.
    """

    urls: set[str] = field(default_factory=set)
    titles: set[tuple[SignalKind, str]] = field(default_factory=set)
    article_titles: set[str] = field(default_factory=set)


def already_seen(
    session: Session, client: Client, *, now: dt.datetime | None = None
) -> Seen:
    """Everything this mandate has that a new signal could be a duplicate of.

    Two halves, and the second is the one the story is about. The stored signals
    are the obvious half. The other is the client's own news — its coverage and
    the market material the topic radar already linked to it — because a study a
    trade publication reported on is in ``articles`` under a headline, and listing
    it again under Studien would show the same document twice with two different
    dates on it.

    Compared on ``canonical_url`` and on the normalized-title hash, which are the
    two identities the article dedup already uses; a signal and an article that
    are the same thing therefore collapse on exactly the rule that would have
    collapsed two articles.

    Only the *news* half is bounded, by :data:`_COVERAGE_LOOKBACK`. It is read once
    per mandate per sweep and would otherwise grow with the archive forever, and an
    article older than a year cannot plausibly be the same document as an item a
    forward calendar is publishing this morning. The stored signals are read whole
    and deliberately so: they carry the UNIQUE the insert would fail on, and a
    window over them would not miss a duplicate — it would turn one into an
    IntegrityError that costs the whole class.
    """
    seen = Seen()
    fresh_enough = (now or dt.datetime.now(dt.UTC)) - _COVERAGE_LOOKBACK
    signals = session.execute(
        select(MarketSignal.url, MarketSignal.title_hash, MarketSignal.kind).where(
            MarketSignal.client_id == client.id
        )
    ).all()
    coverage = session.execute(
        select(Article.url, Article.title_hash)
        .join(Analysis, Analysis.article_id == Article.id)
        .where(Analysis.client_id == client.id, Article.published_at >= fresh_enough)
    ).all()
    material = session.execute(
        select(Article.url, Article.title_hash)
        .join(TopicHit, TopicHit.article_id == Article.id)
        .where(TopicHit.client_id == client.id, Article.published_at >= fresh_enough)
    ).all()
    for url, hashed, kind in signals:
        if url:
            seen.urls.add(canonical_url(url))
        if hashed:
            seen.titles.add((kind, hashed))
    for url, hashed in (*coverage, *material):
        if url:
            seen.urls.add(canonical_url(url))
        if hashed:
            seen.article_titles.add(hashed)
    return seen


def store(
    session: Session,
    client: Client,
    drafts: Sequence[SignalDraft],
    *,
    seen: Seen,
    now: dt.datetime,
) -> list[MarketSignal]:
    """Persist the drafts this mandate does not already have, and return them.

    Commits once at the end, so the sweep's per-class guard can roll back a class
    that failed halfway without touching what the classes before it stored.
    """
    written: list[MarketSignal] = []
    for draft in drafts:
        url_key = canonical_url(draft.url)
        # ``None`` for a headline too thin to trust — the same gate the article
        # dedup applies before collapsing two stories on an exact title match.
        title_key = dedup_title_hash(draft.title, draft.publisher)
        if not url_key or url_key in seen.urls:
            continue
        # Per class against the stored signals, kind-agnostic against the mandate's
        # own news: the same split the two sets carry, for the reasons on ``Seen``.
        if title_key is not None and (
            (draft.kind, title_key) in seen.titles or title_key in seen.article_titles
        ):
            continue
        seen.urls.add(url_key)
        if title_key is not None:
            seen.titles.add((draft.kind, title_key))
        written.append(
            MarketSignal(
                client_id=client.id,
                kind=draft.kind,
                title=draft.title,
                publisher=draft.publisher,
                url=draft.url,
                found_at=now,
                published_at=draft.published_at,
                effective_at=draft.effective_at,
                deadline_at=draft.deadline_at,
                summary=draft.summary,
                origin=draft.origin,
                title_hash=title_key,
            )
        )
    if written:
        session.add_all(written)
        session.commit()
    return written


__all__ = [
    "EventFetcher",
    "FetchFeed",
    "MarketFetcher",
    "MarketSource",
    "RegulationFetcher",
    "Seen",
    "SignalDraft",
    "StudyFetcher",
    "already_seen",
    "fetchers",
    "load_sources",
    "store",
]
