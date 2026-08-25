"""RSS ingestion.

``fetch_feed`` turns a single feed URL into a list of :class:`FeedItem` records,
capturing *only* what the feed already syndicates — headline, link, source, date,
the feed's own summary, and language. It makes exactly one HTTP request (for the
feed document) and never a second one to fetch an article body: the
Leistungsschutzrecht makes scraping paywalled bodies both a legal and a product
non-starter, so that boundary is enforced structurally here (the lone HTTP call
lives in :func:`_fetch_raw`, and the rest of the module only parses bytes).

Fault isolation is the other job of this module. A feed that is unreachable,
times out, or is unparseable logs a WARNING and returns an empty list rather than
raising, so one bad feed can never abort a multi-feed sweep.
"""

from __future__ import annotations

import calendar
import datetime as dt
import html
import http.client
import logging
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass

import feedparser

_log = logging.getLogger(__name__)

# Per-feed request budget. A feed that does not answer within this window is
# treated as unreachable (WARNING + empty) rather than stalling the daily sweep.
# 20s is generous for an RSS document yet bounded enough to keep a sweep of ~40
# feeds moving even when a couple are slow.
_FEED_TIMEOUT_SECONDS = 20.0

# Hard cap on the feed body we will read into memory. RSS/Atom documents are a
# few hundred KB at most; this bounds a hostile or broken feed that streams a
# multi-GB response (or drips bytes forever) so it cannot OOM or stall the sweep.
_MAX_FEED_BYTES = 25 * 1024 * 1024

# A polite, identifying User-Agent. Several German outlets reject the stdlib
# default urllib agent with a 403, so we send our own.
_USER_AGENT = "NewsPulse/0.1 (+local RSS monitor)"


class _FeedTooLargeError(Exception):
    """Raised when a feed body exceeds :data:`_MAX_FEED_BYTES` — a hostile or
    broken feed. Handled exactly like any other fetch failure (WARNING + empty)."""


class _FeedUnparseableError(Exception):
    """A 200 that is not a feed: HTML where XML was expected, and no entries.

    Raised only under ``strict``. This is what a moved RSS path actually returns —
    a "Seite nicht gefunden" page or a relaunch landing page, served with a
    perfectly healthy status code — so a caller that asks to hear about a dead
    source has to hear about this one too, or the commonest way a source dies is
    the one way it stays silent."""


@dataclass(frozen=True, slots=True)
class FeedItem:
    """A single syndicated feed entry, normalized.

    Deliberately holds only feed-provided fields (no body text). ``published_at``
    is always timezone-aware UTC; ``summary`` and ``language`` may be ``None`` when
    the feed omits them.
    """

    title: str
    link: str
    source: str
    published_at: dt.datetime
    summary: str | None
    language: str | None
    # The byline when the feed carries one (RSS <dc:creator>, Atom <author>).
    # Most German feeds omit it, so it is optional and defaults to None — which
    # also keeps every existing construction site valid.
    author: str | None = None


def _utcnow() -> dt.datetime:
    """Timezone-aware UTC now. Wrapped so tests can monkeypatch it."""
    return dt.datetime.now(dt.UTC)


def _ensure_utc(value: dt.datetime) -> dt.datetime:
    """Coerce a datetime to timezone-aware UTC.

    A naive value is assumed to already be UTC (the convention across NewsPulse).
    Callers may hand us a naive ``since``/``fetched_at`` — the Python default —
    and ``published_at`` is always tz-aware, so without this the ``published_at``
    comparison would raise ``TypeError: can't compare offset-naive and
    offset-aware datetimes`` and abort the sweep for every feed at once.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _fetch_raw(url: str, timeout: float) -> bytes:
    """The one and only HTTP request an ingest makes for a feed.

    Kept as its own tiny function so the no-second-request guarantee is testable:
    a test mocks this (or ``urllib.request.urlopen`` beneath it) and asserts it is
    called exactly once, with the feed URL — never again for an article body.
    """
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        # Read one byte past the cap so we can detect (not silently truncate) an
        # oversized body and reject the whole feed rather than parse a fragment.
        raw = response.read(_MAX_FEED_BYTES + 1)
    if len(raw) > _MAX_FEED_BYTES:
        raise _FeedTooLargeError(f"feed body exceeds {_MAX_FEED_BYTES} bytes")
    return raw


def _entry_published_at(
    entry: dict, *, feed_url: str, fetched_at: dt.datetime
) -> dt.datetime:
    """Normalize an entry's publication date to timezone-aware UTC.

    feedparser hands back ``*_parsed`` dates as a ``time.struct_time`` already in
    UTC, so ``calendar.timegm`` (which reads a struct as UTC) yields the correct
    epoch. When a feed omits the date or feedparser cannot parse it, we fall back
    to ``fetched_at`` and log at DEBUG rather than dropping the item.
    """
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        _log.debug(
            "Feed %s entry %r has no parseable date; falling back to fetched_at",
            feed_url,
            entry.get("link"),
        )
        return fetched_at
    return dt.datetime.fromtimestamp(calendar.timegm(parsed), tz=dt.UTC)


def _entry_summary(entry: dict) -> str | None:
    """The feed-provided summary/description, or ``None`` when blank.

    RSS ``<description>`` and Atom ``<summary>`` both land in ``entry.summary``.
    An Atom entry that carries only ``<content>`` (no ``<summary>``) still has
    feed-provided text the acceptance criteria expect us to capture, so we fall
    back to the first ``<content>`` value. This is the *only* body-ish text
    captured — it is what the feed itself syndicates, never the full article.
    """
    raw = entry.get("summary")
    if not isinstance(raw, str) or not raw.strip():
        raw = _first_content_value(entry)
    if not isinstance(raw, str):
        return None
    return _plain_text(raw)


# Feed descriptions are frequently HTML. Google News in particular emits a
# summary that is *only* an <a> wrapper around the headline, which carries no
# information but would otherwise be stored, shown in the dashboard, and sent to
# the analyzer as if it were a summary.
_MARKUP_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


# How much text a summary must add beyond the headline to be worth keeping.
# Google News emits the headline plus the outlet name as its "summary"; a real
# summary says something the headline did not.
_MIN_SUMMARY_GAIN = 30
_SUMMARY_NOISE_RE = re.compile(r"[^0-9a-zà-ÿ]+")


def _summary_unless_echo(summary: str | None, title: str) -> str | None:
    """Drop a summary that only restates the headline.

    An aggregator (and some publisher feeds) sets ``<description>`` to the
    headline itself, sometimes with the outlet appended. Stored as-is it would
    show the same sentence twice in the dashboard and spend analyzer tokens
    re-reading the title. Anything that genuinely adds text is kept.
    """
    if summary is None:
        return None
    norm_summary = _SUMMARY_NOISE_RE.sub("", summary.casefold())
    norm_title = _SUMMARY_NOISE_RE.sub("", title.casefold())
    if not norm_title or not norm_summary:
        return summary or None
    if norm_summary.startswith(norm_title):
        remainder = len(norm_summary) - len(norm_title)
        if remainder < _MIN_SUMMARY_GAIN:
            return None
    return summary


# A byline is only useful if it names a person. Feeds routinely put the outlet,
# a desk, or a wire code in the author field ("dpa", "Redaktion", "SZ.de"),
# which would pollute a media list with non-people.
_NON_PERSON_BYLINES = frozenset(
    {"dpa", "afp", "reuters", "redaktion", "online redaktion", "red", "kna", "epd",
     "sid", "dpa-afx", "pressemitteilung", "gastbeitrag", "webredaktion"}
)


def _entry_author(entry: dict, source: str) -> str | None:
    """The article's byline, or ``None`` when the feed has no usable one.

    feedparser folds RSS ``<dc:creator>`` and Atom ``<author>`` into
    ``entry.author``. A byline that merely repeats the outlet, names a desk, or
    is a wire code is dropped: the point of storing this is to learn which
    *journalists* cover a client, and those values would only add noise.
    """
    raw = entry.get("author")
    if not isinstance(raw, str):
        return None
    name = _WHITESPACE_RE.sub(" ", raw).strip(" ,;|-")
    if not name or len(name) > 120:
        return None
    key = name.casefold()
    if key in _NON_PERSON_BYLINES or key == (source or "").casefold():
        return None
    return name


def _entry_source(entry: dict, feed_source: str, per_entry: bool) -> str:
    """The outlet to credit for one entry.

    Normally the feed itself is the outlet, so every item carries ``feed_source``.
    An *aggregator* feed (a Google News query) is not the publisher: each entry
    names its own outlet in RSS ``<source>``, and crediting them all to the
    aggregator would put the wrong byline on every story and collapse dozens of
    real outlets into one name in the archive. Opt-in, because for an ordinary
    feed a stray ``<source>`` element should not override the registry's name.
    """
    if not per_entry:
        return feed_source
    raw = entry.get("source")
    title = raw.get("title") if isinstance(raw, dict) else None
    if isinstance(title, str) and title.strip():
        return title.strip()
    return feed_source


def _plain_text(raw: str) -> str | None:
    """Markup-free, whitespace-collapsed text, or ``None`` when nothing is left.

    Tags are dropped rather than escaped: what remains is the text a reader would
    see, which is what both the dashboard and the analyzer want. A description
    consisting solely of markup collapses to ``None`` rather than to a fragment of
    HTML masquerading as a summary.
    """
    # Order matters: strip tags, *then* unescape, *then* collapse whitespace.
    # Unescaping last would leave &nbsp; as literal text; collapsing before
    # unescaping would leave the resulting \xa0 runs uncollapsed.
    text = _MARKUP_TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    return _WHITESPACE_RE.sub(" ", text).strip() or None


def _first_content_value(entry: dict) -> str | None:
    """The first Atom ``<content>`` value, when present. feedparser stores content
    as a list of dicts each with a ``value`` key."""
    contents = entry.get("content")
    if isinstance(contents, list) and contents:
        value = contents[0].get("value")
        if isinstance(value, str):
            return value
    return None


def _parse_items(
    raw: bytes,
    *,
    url: str,
    since: dt.datetime,
    source: str | None,
    fetched_at: dt.datetime,
    per_entry_source: bool = False,
    strict: bool = False,
) -> list[FeedItem]:
    """Parse feed bytes into items published strictly after ``since``.

    ``since`` and ``fetched_at`` must already be timezone-aware UTC (the caller
    normalizes them). Kept separate from :func:`fetch_feed` so the fetch and the
    parse/normalize phases each sit behind their own isolation boundary.

    ``strict`` has the meaning it has in :func:`fetch_feed`: a body that is not a
    feed is raised rather than logged away, because that — not a connection error
    — is what a source that moved usually looks like from here.
    """
    parsed = feedparser.parse(raw)
    # feedparser is lenient and sets ``bozo`` for any not-well-formed document.
    # Many real feeds are technically bozo yet still parse into entries, so we only
    # treat bozo as a failure when it produced *nothing* to work with.
    if parsed.bozo and not parsed.entries:
        if strict:
            raise _FeedUnparseableError(f"{url} is not a feed ({parsed.get('bozo_exception')})")
        _log.warning(
            "Feed %s is malformed (%s); skipping",
            url,
            parsed.get("bozo_exception"),
        )
        return []

    feed_source = source or parsed.feed.get("title") or url
    feed_language = parsed.feed.get("language")

    items: list[FeedItem] = []
    for entry in parsed.entries:
        published_at = _entry_published_at(entry, feed_url=url, fetched_at=fetched_at)
        # "newer than since" — an item published exactly at ``since`` is excluded
        # so an incremental sweep using the last-seen timestamp never re-ingests
        # the boundary item.
        if published_at <= since:
            continue
        title = (entry.get("title") or "").strip()
        item_source = _entry_source(entry, feed_source, per_entry_source)
        items.append(
            FeedItem(
                title=title,
                link=(entry.get("link") or "").strip(),
                source=item_source,
                published_at=published_at,
                summary=_summary_unless_echo(_entry_summary(entry), title),
                language=feed_language,
                author=_entry_author(entry, item_source),
            )
        )
    return items


def fetch_feed(
    url: str,
    since: dt.datetime,
    *,
    source: str | None = None,
    fetched_at: dt.datetime | None = None,
    timeout: float = _FEED_TIMEOUT_SECONDS,
    per_entry_source: bool = False,
    strict: bool = False,
) -> list[FeedItem]:
    """Fetch and parse one feed, returning items published newer than ``since``.

    ``source`` labels the emitted items (defaults to the feed's own channel title,
    then the URL); ``fetched_at`` is the timestamp used both as the dateless-item
    fallback and to stamp "now" for the fetch (defaults to the current UTC time).
    ``since`` and ``fetched_at`` may be naive — a naive value is read as UTC.

    ``per_entry_source`` credits each item to the outlet named in its own RSS
    ``<source>`` element instead of to the feed — correct for an aggregator feed
    (see :mod:`newspulse.gnews`), wrong for an ordinary publisher feed.

    Returns an empty list — never raises — when the feed is unreachable, times
    out, is oversized, or is unparseable, logging a WARNING so a single bad feed
    never aborts a multi-feed sweep.

    ``strict`` inverts that last part and re-raises instead. The news sweep reads
    forty publications and one of them being down is not the day's story, so it
    takes the default. A market class (:mod:`newspulse.market_sources`) has one or
    two sources, and a silent empty list from the regulatory calendar is
    indistinguishable from a quiet fortnight — which is the one thing a forward
    calendar must never be wrong about. Those callers ask for the failure so the
    sweep's per-class guard can log it at ERROR.

    "Failure" there covers the body as well as the connection. A moved RSS path
    rarely answers with an error — it answers 200 with an HTML landing page — so
    under ``strict`` a body that parses into no entries at all raises
    :class:`_FeedUnparseableError` rather than logging a WARNING and returning
    nothing, which would have left the commonest way a source dies as the one way
    it dies quietly. A feed that is genuinely well-formed and merely has nothing
    new is untouched by this: it parsed into entries, and ``since`` filtered them.
    """
    # Normalize to tz-aware UTC up front so the ``published_at`` comparison (which
    # is always tz-aware) can never raise a naive/aware TypeError mid-sweep.
    since = _ensure_utc(since)
    fetched_at = _ensure_utc(fetched_at or _utcnow())

    try:
        raw = _fetch_raw(url, timeout)
    except _FeedTooLargeError as exc:
        if strict:
            raise
        _log.warning("Feed %s is too large (%s); skipping", url, exc)
        return []
    except (
        urllib.error.URLError,
        socket.timeout,
        TimeoutError,
        OSError,
        http.client.HTTPException,
    ) as exc:
        # Unreachable / timed out / connection reset / truncated or garbled
        # response (IncompleteRead, BadStatusLine) — isolate and move on.
        if strict:
            raise
        _log.warning("Feed %s could not be fetched (%s); skipping", url, exc)
        return []

    try:
        return _parse_items(
            raw,
            url=url,
            since=since,
            source=source,
            fetched_at=fetched_at,
            per_entry_source=per_entry_source,
            strict=strict,
        )
    except Exception as exc:  # noqa: BLE001 — structural fault-isolation boundary
        # Any unexpected error while parsing or normalizing one feed must not
        # abort the sweep. This is the deliberate broad-catch the acceptance
        # criteria require; it always logs, never swallows silently.
        if strict:
            raise
        _log.warning("Feed %s could not be parsed (%s); skipping", url, exc)
        return []


__all__ = ["FeedItem", "fetch_feed"]
