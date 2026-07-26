"""Client matching and deduplication — the cheap pre-filter before Claude.

This module does the two mechanical steps that stand between raw feed items and
the (expensive, authoritative) Claude analysis:

* :func:`match_candidates` pairs each feed item with every client whose name,
  alias, or keyword appears in the item's headline or feed summary, using
  case-insensitive *word-boundary* matching so a German compound noun
  ("Autobahn") never counts as a hit for a shorter company name ("Bahn").

* :func:`deduplicate` drops items already stored (by URL) and collapses the
  near-identical wire copy that German outlets republish from dpa — same headline
  under a different link and a different outlet byline — down to a single stored
  article, choosing the retained copy deterministically so re-runs are stable.

Design note, load-bearing — do NOT "tighten" the matcher:
    The pre-filter deliberately favours *recall over precision*. Its only job is
    to cheaply narrow tens of thousands of daily items down to the handful that
    even mention a client, so Claude (which actually reads each candidate) has a
    small enough set to analyse. A name collision that survives this filter
    ("Otto" the retailer vs. "Otto" the first name) is expected and fine — Claude
    disambiguates it downstream. Making the rule stricter here to cut false
    matches would instead silently drop *real* coverage before anything ever
    reads it, which is the one failure this product cannot recover from. If you
    are tempted to add a stricter rule, add it to the analysis prompt, not here.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
from collections.abc import Collection, Sequence
from typing import NamedTuple

from .ingest import FeedItem
from .models import Client

_log = logging.getLogger(__name__)

# --- Named constants (the "why" lives next to each) ----------------------------

# A byline after an unambiguous separator (pipe/middot — "… | Handelsblatt") is
# short: an outlet name, not a clause. Capping the stripped tail at 5 words keeps a
# long trailing phrase after such a separator from being swallowed as if it were a
# byline. (Dash separators are guarded differently — see _strip_source_suffix.)
_MAX_SOURCE_SUFFIX_WORDS = 5

# Separators German outlets use to append their name to a headline. Pipe and middot
# are unambiguous byline markers; the dash variants (hyphen, en-dash, em-dash) are
# ALSO used mid-headline as a real Gedankenstrich ("Mercedes – Absatz bricht ein"),
# so they are handled conservatively below (stripped only when the tail is the source).
_SOURCE_SEP_RE = re.compile(r"\s+([-–—|·])\s+")

# Of those, only pipe and middot reliably precede an outlet byline — German headlines
# never use them mid-sentence — so a short tail after one *may* be a byline. The dash
# variants stay source-guarded (see _strip_source_suffix): a false collapse drops a
# real story, the costlier error, so dedup errs toward keeping.
_STRONG_BYLINE_SEPS = frozenset("|·")

# The fewest word-tokens a headline needs before dedup treats it as a specific story.
# Below this it is an empty/symbol-only title, a section rubric ("Formel 1 · …",
# "Liveticker | …"), or PR boilerplate ("Pressemitteilung", "Quartalszahlen
# veröffentlicht") that unrelated stories coincidentally share — too little signal to
# read an exact title match across different URLs as republished wire copy. It gates
# two decisions: whether a short pipe/middot tail is an outlet byline (vs a rubric
# whose short *head* is the section label), and whether an exact-title match may
# hash-collapse at all. Wire copy is a full clause (subject+verb+object ≈ 4 words) and
# clears it easily; a lower bar would collapse — and silently drop — a real, distinct
# story, the one dedup error this module cannot recover from.
_MIN_HEADLINE_WORDS = 4

# Word tokens of a multi-word client term are joined by a run of non-alphanumerics
# (not whitespace alone) so "Deutsche Bank" still matches a hyphenated "Deutsche-Bank"
# in a headline — another nudge toward recall (see the module docstring).
_TERM_TOKEN_GAP = r"[\W_]+"

# Everything that is not a Unicode letter or digit (punctuation, whitespace,
# underscore) is stripped when normalizing a title, so only the "word content"
# survives into the dedup hash.
_NON_ALNUM_RE = re.compile(r"[\W_]+", re.UNICODE)

# A tz-aware lower bound used only as a sort fallback for an item that is somehow
# missing ``published_at``; keeps the deterministic ordering from crashing on a
# naive/aware or None comparison.
_MIN_PUBLISHED_AT = dt.datetime.min.replace(tzinfo=dt.UTC)


class Candidate(NamedTuple):
    """A candidate (item, client) pair the pre-filter surfaced for analysis.

    A NamedTuple so callers can either unpack it (``for item, client in …``) or
    read the fields by name. One item that mentions two clients yields two
    Candidates sharing the same ``item`` — the same story really can be about two
    portfolio companies — which NP-06 stores as one article with two analyses.
    """

    item: FeedItem
    client: Client


# --- Matching ------------------------------------------------------------------


def match_candidates(
    items: Sequence[FeedItem], clients: Sequence[Client]
) -> list[Candidate]:
    """Pair each item with every client it plausibly mentions.

    A client "matches" an item when the client's name, any alias, or any keyword
    occurs — case-insensitively and on a word boundary — in the item's title or
    feed summary. See the module docstring: this is a deliberately loose recall
    filter, not the final relevance decision.
    """
    # One compiled matcher per client, built once, then searched against every
    # item — the cheap part of "cheap pre-filter".
    matchers = [(client, _compile_client_matcher(client)) for client in clients]
    candidates: list[Candidate] = []
    for item in items:
        # Case-fold once per item, not per client: this is the ß→ss fold, so a
        # client "Straße" matches a headline "STRASSE" (the matchers are compiled
        # from case-folded terms too — see _term_pattern).
        haystack = _haystack(item).casefold()
        if not haystack.strip():
            continue
        for client, matcher in matchers:
            if matcher is not None and matcher.search(haystack):
                candidates.append(Candidate(item=item, client=client))
    return candidates


def _haystack(item: FeedItem) -> str:
    """The searchable text for an item: its title plus the feed-provided summary.

    Reads ``summary`` (FeedItem) and falls back to ``summary_text`` (a stored
    Article), so the matcher works on either object. Only feed-syndicated text is
    ever searched — no article body is fetched (no-scrape rule)."""
    title = getattr(item, "title", "") or ""
    summary = getattr(item, "summary", None)
    if summary is None:
        summary = getattr(item, "summary_text", None)
    return f"{title}\n{summary or ''}"


def _compile_client_matcher(client: Client) -> re.Pattern[str] | None:
    """Compile a single case-folded, word-boundary matcher for a client.

    All of a client's terms (name + aliases + keywords) become one alternation
    wrapped in ``(?<!\\w) … (?!\\w)`` lookarounds. The lookarounds — rather than
    ``\\b`` — are what reject a substring inside a longer word: a term only
    matches when the characters immediately around it are not word characters, so
    "Bahn" hits "Die Bahn fährt" but never "Autobahn". The pattern is built from
    case-folded terms (and the caller case-folds the haystack), giving full Unicode
    case-insensitivity including the German ß→ss fold, so "Straße" matches "STRASSE"
    — a recall gap that ``re.IGNORECASE`` alone would leave open. Returns ``None``
    for a client with no usable terms so the caller can skip it."""
    terms = _client_terms(client)
    if not terms:
        return None
    alternation = "|".join(_term_pattern(term) for term in terms)
    return re.compile(rf"(?<!\w)(?:{alternation})(?!\w)")


def _client_terms(client: Client) -> list[str]:
    """Name + aliases + keywords, trimmed, blanks dropped, case-folded-deduped."""
    raw = [
        getattr(client, "name", "") or "",
        *(getattr(client, "aliases", None) or []),
        *(getattr(client, "keywords", None) or []),
    ]
    terms: list[str] = []
    seen: set[str] = set()
    for value in raw:
        term = (value or "").strip()
        if not term:
            continue
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        terms.append(term)
    return terms


def _term_pattern(term: str) -> str:
    """Escape a term into a regex fragment, allowing a flexible internal gap.

    A multi-word term ("Deutsche Bank") joins its escaped tokens with
    ``_TERM_TOKEN_GAP`` (a run of non-alphanumerics) so it still matches when the
    feed rendered the gap as a newline, double space, or hyphen ("Deutsche-Bank") —
    another nudge toward recall. The term is case-folded so the matcher shares the
    ß→ss fold with the haystack. Each token is ``re.escape``-d so punctuation in a
    company name ("E.ON", "1&1") is treated literally, not as regex syntax."""
    return _TERM_TOKEN_GAP.join(re.escape(token) for token in term.casefold().split())


# --- Deduplication -------------------------------------------------------------


def deduplicate(
    items: Sequence[FeedItem],
    *,
    known_urls: Collection[str] | None = None,
    known_title_hashes: Collection[str] | None = None,
) -> list[FeedItem]:
    """Return the items worth storing: no URL already seen, no near-duplicate.

    Two collapses happen in one pass:

    * **URL** — an item whose link is in ``known_urls`` (already stored) or
      already kept in this batch is dropped; this mirrors the DB's UNIQUE(url).
    * **Normalized title hash** — dpa wire copy republished across outlets shares
      a headline but not a URL; items whose normalized-title hash is in
      ``known_title_hashes`` or already kept collapse to one stored article.

    The retained copy is deterministic: items are processed earliest-``published_at``
    first, ties broken by source name (then URL), and the first survivor of each
    URL/hash wins — so a second run over the same feeds keeps exactly the same
    copy and adds nothing new.

    Title-hash collapse is *only* applied to titles specific enough to trust
    (``>= _MIN_HEADLINE_WORDS`` word-tokens after byline removal). An empty or
    symbol-only headline, or a short generic label two unrelated firms happen to
    share, falls back to URL-only dedup — collapsing it would silently drop a
    real, distinct story before Claude ever reads it, the one dedup error this
    module cannot recover from.
    """
    seen_urls: set[str] = set(known_urls or ())
    seen_hashes: set[str] = set(known_title_hashes or ())

    kept: list[FeedItem] = []
    dropped = 0
    linkless = 0
    for item in sorted(items, key=_dedup_sort_key):
        url = _item_url(item)
        if not url:
            # No link => no stable identity: the item can't be deduplicated across
            # runs and can't be stored (articles.url is UNIQUE and NOT NULL), so a
            # second linkless item would collide on url="" and roll back the whole
            # persist batch. Drop it here — logged, never silent — rather than let one
            # dateless/linkless entry sink every other kept article and fail the run.
            linkless += 1
            continue
        # None => this title is too thin to hash-collapse; dedup it by URL only.
        thash = dedup_title_hash(_item_title(item), _item_source(item))
        if url in seen_urls or (thash is not None and thash in seen_hashes):
            dropped += 1
            continue
        kept.append(item)
        seen_urls.add(url)
        if thash is not None:
            seen_hashes.add(thash)

    if dropped:
        _log.debug("deduplicate: kept %d item(s), dropped %d duplicate(s)", len(kept), dropped)
    if linkless:
        _log.warning("deduplicate: dropped %d item(s) with no link (unstorable)", linkless)
    return kept


def title_hash(title: str, source: str | None = None) -> str:
    """Stable hash of the normalized title, for the ``articles.title_hash`` column.

    SHA-256 hex is 64 chars, matching the ``String(64)`` column exactly. NP-06
    persists this alongside each article so a later run can seed
    ``known_title_hashes`` and recognise an already-stored wire story."""
    return hashlib.sha256(normalize_title(title, source).encode("utf-8")).hexdigest()


def dedup_title_hash(title: str, source: str | None = None) -> str | None:
    """The collapse hash for a title, or ``None`` when it is too thin to trust.

    Returns :func:`title_hash` only when the byline-stripped title carries at least
    ``_MIN_HEADLINE_WORDS`` word-tokens — the same gate :func:`deduplicate` applies
    before collapsing an exact cross-URL title match as republished wire copy. Below
    that bar an exact match is too weak to trust (a symbol-only title, or PR
    boilerplate two firms share), so identity falls back to URL only. Exposed so the
    daily job can resolve a collapsed near-duplicate copy back to the stored article
    it was folded into, using exactly the rule that folded it."""
    if _significant_word_count(title, source) >= _MIN_HEADLINE_WORDS:
        return title_hash(title, source)
    return None


def normalize_title(title: str, source: str | None = None) -> str:
    """Normalize a headline for dedup: source byline removed, then reduced to its
    lower-cased alphanumeric core (punctuation and whitespace stripped)."""
    return _NON_ALNUM_RE.sub("", _strip_source_suffix(title, source).casefold())


def _strip_source_suffix(title: str, source: str | None) -> str:
    """Drop a trailing outlet byline ("… | Handelsblatt", "… – SPIEGEL ONLINE").

    Splits on the *last* source separator and strips the tail only when it is
    clearly an outlet byline, never part of the story:

    * **Any separator** — strip when the tail's alnum core equals this item's
      ``source`` (the byline literally is this outlet's name).
    * **Pipe/middot only** — also strip a short tail (``<= _MAX_SOURCE_SUFFIX_WORDS``)
      when the *head* is itself a full headline (``>= _MIN_HEADLINE_WORDS``). The
      head guard rejects a *rubric* prefix ("Formel 1 · Verstappen siegt",
      "Liveticker | Bayern gewinnt") whose short head is a section label and whose
      tail is the actual story: stripping there would collapse two distinct stories
      that share only the rubric.

    A dash separator (hyphen, en-dash, em-dash) is never stripped on length alone —
    the en-dash is the German Gedankenstrich used mid-headline ("Mercedes – Absatz
    bricht ein"), so it is removed only via the source-name match above. Erring
    toward keeping a duplicate beats collapsing two distinct stories into one and
    dropping a real article (the costlier dedup error)."""
    matches = list(_SOURCE_SEP_RE.finditer(title))
    if not matches:
        return title.strip()
    last = matches[-1]
    head = title[: last.start()].strip()
    tail = title[last.end() :].strip()
    if not head:
        return title.strip()
    if source and _alnum(tail) == _alnum(source.strip()):
        return head
    sep_char = last.group(1)
    if (
        sep_char in _STRONG_BYLINE_SEPS
        and len(tail.split()) <= _MAX_SOURCE_SUFFIX_WORDS
        and len(head.split()) >= _MIN_HEADLINE_WORDS
    ):
        return head
    return title.strip()


def _significant_word_count(title: str, source: str | None) -> int:
    """Count of alphanumeric word-tokens in the byline-stripped title.

    Zero for an empty/symbol-only headline; small for a short generic label. Used
    by :func:`deduplicate` to decide whether a title carries enough signal to
    trust an exact cross-URL match as republished wire copy — see the
    ``_MIN_HEADLINE_WORDS`` rationale."""
    return len(re.findall(r"\w+", _strip_source_suffix(title, source)))


def _alnum(value: str) -> str:
    """Lower-cased alphanumeric core of a string (used to compare byline tails)."""
    return _NON_ALNUM_RE.sub("", value.casefold())


def _dedup_sort_key(item: FeedItem) -> tuple[dt.datetime, str, str]:
    """Deterministic order: earliest published_at, then source, then URL."""
    return (
        getattr(item, "published_at", None) or _MIN_PUBLISHED_AT,
        _item_source(item),
        _item_url(item),
    )


def _item_url(item: FeedItem) -> str:
    """The item's link. ``link`` on a FeedItem, ``url`` on a stored Article."""
    return (getattr(item, "url", None) or getattr(item, "link", None) or "").strip()


def _item_title(item: FeedItem) -> str:
    return getattr(item, "title", "") or ""


def _item_source(item: FeedItem) -> str:
    return getattr(item, "source", "") or ""


__all__ = [
    "Candidate",
    "dedup_title_hash",
    "deduplicate",
    "match_candidates",
    "normalize_title",
    "title_hash",
]
