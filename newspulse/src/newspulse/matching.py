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
import urllib.parse
from collections.abc import Collection, Sequence
from typing import NamedTuple

from . import company_names
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
    """Name + aliases + keywords, trimmed, blanks dropped, case-folded-deduped.

    Each name and alias also contributes its legal-form-free variant (see
    :mod:`newspulse.company_names`), so a client entered as "IB-7 Beauty Tech
    GmbH" still matches the headline that writes "IB-7 Beauty Tech". Keywords are
    topics, not company names, so they are taken as typed.
    """
    raw = [
        *company_names.variants(getattr(client, "name", "") or ""),
        *(
            variant
            for alias in (getattr(client, "aliases", None) or [])
            for variant in company_names.variants(alias or "")
        ),
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
    # Compared in canonical form so a stored link and the same link carrying a
    # tracking parameter are one identity. Callers may pass raw stored URLs.
    seen_urls: set[str] = {canonical_url(u) for u in (known_urls or ())}
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
        identity = canonical_url(url)
        if identity in seen_urls or (thash is not None and thash in seen_hashes):
            dropped += 1
            continue
        kept.append(item)
        seen_urls.add(identity)
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


# Query parameters that identify the *referral*, not the article. German outlets
# routinely append these to their RSS links, so the same story arrives at two
# spellings of one URL. Prefix families (utm_*) plus a few standalone names.
_TRACKING_PARAM_PREFIXES = ("utm_", "at_", "wt_")
_TRACKING_PARAMS = frozenset(
    {"ref", "fbclid", "gclid", "igshid", "mc_cid", "mc_eid", "cmpid", "ncid", "src"}
)


def _is_tracking_param(name: str) -> bool:
    key = name.casefold()
    return key in _TRACKING_PARAMS or key.startswith(_TRACKING_PARAM_PREFIXES)


def canonical_url(url: str) -> str:
    """The identity form of a URL for deduplication.

    Collapses the spellings that mean "the same page": scheme (http/https), a
    ``www.`` host prefix, host case, a trailing slash, the fragment, and referral
    tracking parameters. Real query parameters are preserved and re-sorted, since
    for many outlets the article id lives there (``?id=123``) and dropping it
    would fold unrelated stories together.

    This is an identity used for comparison only — the article is still stored
    and linked out under the exact URL the feed gave, so the reader always
    follows the publisher's own link.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    parsed = urllib.parse.urlsplit(raw)
    # A scheme-less/relative link has no reliable identity to normalize; compare
    # it as-is rather than inventing a host for it.
    if not parsed.scheme or not parsed.netloc:
        return raw

    host = parsed.netloc.casefold()
    host = host.removeprefix("www.")
    for scheme, port in (("http", ":80"), ("https", ":443")):
        if parsed.scheme.casefold() == scheme:
            host = host.removesuffix(port)

    kept = [
        (name, value)
        for name, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if not _is_tracking_param(name)
    ]
    query = urllib.parse.urlencode(sorted(kept))

    path = parsed.path.rstrip("/")
    # Scheme is dropped entirely (not normalized to https): http:// and https://
    # of the same page are the same article, and the fragment never identifies a
    # different story.
    return f"{host}{path}" + (f"?{query}" if query else "")


def _item_title(item: FeedItem) -> str:
    return getattr(item, "title", "") or ""


def _item_source(item: FeedItem) -> str:
    return getattr(item, "source", "") or ""


def name_matcher(client: Client) -> re.Pattern[str] | None:
    """Match a client's *name and aliases* — never its themes.

    Deliberately not :func:`_compile_client_matcher`, which also folds in the
    keywords because its job is "does this article concern the client at all".
    This one answers a different question, asked by the topic radar: is this hit
    about the client, or about its market? A theme found the item, so the theme is
    present in both kinds by construction and would classify everything as "about
    the client".

    Returns ``None`` for a client with no usable name, so callers can treat
    "cannot tell" as "not about the client" rather than crashing.
    """
    return terms_matcher(
        [
            variant
            for raw in [getattr(client, "name", ""), *(getattr(client, "aliases", None) or [])]
            for variant in company_names.variants((raw or "").strip())
        ]
    )


def terms_matcher(terms: Sequence[str]) -> re.Pattern[str] | None:
    """One case-folded, word-boundary matcher over ``terms``, or ``None`` if empty.

    The shared shape behind the name, theme and field matchers: the lookarounds
    are what stop "Mode" matching "Modernisierung", and every caller wants that.
    """
    cleaned = [term.strip() for term in terms if term and term.strip()]
    if not cleaned:
        return None
    alternation = "|".join(_term_pattern(t) for t in dict.fromkeys(cleaned))
    return re.compile(rf"(?<!\w)(?:{alternation})(?!\w)")


def theme_matcher(client: Client) -> re.Pattern[str] | None:
    """Match a client's *themes* — its keywords and alert topics, never its name.

    The mirror image of :func:`name_matcher`, and the pair is the whole point: one
    asks "is this about the client", the other "is this about the client's field".
    Market material is what answers yes to the second and no to the first.

    Same word-boundary lookarounds as the client matcher, so "Mode" matches "Mode
    im Wandel" and never "Modernisierung".
    """
    return terms_matcher(
        [
            *(getattr(client, "keywords", None) or []),
            *(getattr(client, "alert_topics", None) or []),
        ]
    )


def radar_matcher(client: Client) -> re.Pattern[str] | None:
    """Match a client's themes *loosely enough for a headline*.

    :func:`theme_matcher` demands the whole phrase, and the press almost never
    repeats one: a mandate whose theme is "KI in der Kosmetik" is written about
    under "Kosmetik". So each theme also contributes its longest word, with a
    five-character floor that keeps stopwords and initialisms out of the
    alternation — a two-letter token would match half the German press.

    Used wherever a stored radar hit has to prove it belongs to this mandate
    rather than to whichever other mandate's search first found the article.
    """
    probes: list[str] = []
    for raw in [*(getattr(client, "alert_topics", None) or []),
                *(getattr(client, "keywords", None) or [])]:
        term = (raw or "").strip()
        if not term:
            continue
        probes.append(term)
        words = [w for w in re.findall(r"\w+", term, re.UNICODE) if len(w) >= 5]
        if words:
            probes.append(max(words, key=len))
    return terms_matcher(probes)


def on_theme(item, matcher: re.Pattern[str] | None) -> bool:
    """Whether ``item``'s syndicated text carries one of those themes."""
    if matcher is None:
        return True
    return matcher.search(_haystack(item).casefold()) is not None


def mentions_client(item, matcher: re.Pattern[str] | None) -> bool:
    """Whether ``item``'s feed-provided text names the client ``matcher`` was built
    from. Only syndicated text is searched — no body is fetched (no-scrape rule)."""
    if matcher is None:
        return False
    return matcher.search(_haystack(item).casefold()) is not None


__all__ = [
    "Candidate",
    "radar_matcher",
    "on_theme",
    "canonical_url",
    "dedup_title_hash",
    "deduplicate",
    "match_candidates",
    "mentions_client",
    "name_matcher",
    "terms_matcher",
    "theme_matcher",
    "normalize_title",
    "title_hash",
]
