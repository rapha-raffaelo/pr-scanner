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
import logging
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

# A polite, identifying User-Agent. Several German outlets reject the stdlib
# default urllib agent with a 403, so we send our own.
_USER_AGENT = "NewsPulse/0.1 (+local RSS monitor)"


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


def _utcnow() -> dt.datetime:
    """Timezone-aware UTC now. Wrapped so tests can monkeypatch it."""
    return dt.datetime.now(dt.UTC)


def _fetch_raw(url: str, timeout: float) -> bytes:
    """The one and only HTTP request an ingest makes for a feed.

    Kept as its own tiny function so the no-second-request guarantee is testable:
    a test mocks this (or ``urllib.request.urlopen`` beneath it) and asserts it is
    called exactly once, with the feed URL — never again for an article body.
    """
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


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
    """The feed-provided summary/description, or ``None`` when blank. This is the
    *only* body-ish text captured — never the full article."""
    raw = entry.get("summary")
    if not isinstance(raw, str):
        return None
    stripped = raw.strip()
    return stripped or None


def fetch_feed(
    url: str,
    since: dt.datetime,
    *,
    source: str | None = None,
    fetched_at: dt.datetime | None = None,
    timeout: float = _FEED_TIMEOUT_SECONDS,
) -> list[FeedItem]:
    """Fetch and parse one feed, returning items published at or after ``since``.

    ``source`` labels the emitted items (defaults to the feed's own channel title,
    then the URL); ``fetched_at`` is the timestamp used both as the dateless-item
    fallback and to stamp "now" for the fetch (defaults to the current UTC time).

    Returns an empty list — never raises — when the feed is unreachable, times
    out, or is unparseable, logging a WARNING so a single bad feed never aborts a
    multi-feed sweep.
    """
    fetched_at = fetched_at or _utcnow()

    try:
        raw = _fetch_raw(url, timeout)
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
        # Unreachable / timed out / connection reset — isolate and move on.
        _log.warning("Feed %s could not be fetched (%s); skipping", url, exc)
        return []

    parsed = feedparser.parse(raw)
    # feedparser is lenient and sets ``bozo`` for any not-well-formed document.
    # Many real feeds are technically bozo yet still parse into entries, so we only
    # treat bozo as a failure when it produced *nothing* to work with.
    if parsed.bozo and not parsed.entries:
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
        if published_at < since:
            continue
        items.append(
            FeedItem(
                title=(entry.get("title") or "").strip(),
                link=(entry.get("link") or "").strip(),
                source=feed_source,
                published_at=published_at,
                summary=_entry_summary(entry),
                language=feed_language,
            )
        )
    return items


__all__ = ["FeedItem", "fetch_feed"]
