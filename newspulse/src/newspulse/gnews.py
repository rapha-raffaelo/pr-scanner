"""Per-client Google News queries as an additional feed source.

The registry in ``feeds_default.toml`` is client-agnostic: it fetches a fixed set
of outlets and *then* asks which stories happen to mention a client. That misses
any coverage outside those 44 publications — which is most regional and trade
press, exactly where a plant closure or a local dispute surfaces first.

This module inverts it: one Google News RSS *search* per active client, asking
for every mention of that client's name and aliases. It is additive — the
registry feeds still run, and the two overlap heavily. That overlap is harmless
because the same story arriving from both routes collapses in deduplication on
its normalized title (the URLs differ, so the title hash is what catches it).

Three properties of this source shape the code here:

* **Links are Google redirects.** ``news.google.com/rss/articles/CBMi…`` rather
  than the publisher's URL, so URL-identity never matches a copy from a direct
  feed and the title hash is the only axis that can collapse them.
* **The publisher is per entry.** Each item names its real outlet in RSS
  ``<source>``; crediting them all to "Google News" would put a false byline on
  every story (hence ``per_entry_source``).
* **There is no real summary.** Google emits an ``<a>`` wrapper around the
  headline, which ``ingest._plain_text`` reduces to ``None`` rather than storing
  markup that only repeats the title.

Results are still run through the normal matcher and the analyzer's relevance
judgment: a name search returns plenty that does not concern the client (another
firm sharing a word, a passing mention), and nothing here is trusted to have
established relevance simply because Google returned it.
"""

from __future__ import annotations

import re
import urllib.parse

from . import company_names
from .feeds import Feed
from .models import Client

# The public Google News RSS search endpoint.
_SEARCH_URL = "https://news.google.com/rss/search"

# German-language, German edition by default. `ceid` is the country:language pair
# Google uses to pick an edition; it must agree with hl/gl or the edition silently
# falls back to US English.
_LANG = "de"
_COUNTRY = "DE"

# The editions a client's own country selects. Stored on every client since day
# one and, until now, never used for anything: an Austrian company was searched in
# the German edition, which ranks Austrian outlets below German ones and buries the
# regional coverage that is usually the first to write about them. Measured on a
# Viennese mandate, the AT edition put Vienna.at and cash.at at the top of the same
# result set the DE edition had pushed down.
#
# Only the German-speaking ones are mapped: an edition for a language the analyzer
# and the registry do not serve would return coverage nobody here can use.
_EDITIONS = {
    "DE": ("de", "DE"),
    "AT": ("de", "AT"),
    "CH": ("de", "CH"),
}


def edition_for(client: Client) -> tuple[str, str]:
    """The (language, country) edition to search for this client.

    Falls back to the German edition for a country with no mapping, rather than
    guessing a language: a wrong edition is worse than the default one, because it
    silently changes what is found without saying so.
    """
    return _EDITIONS.get((getattr(client, "country", "") or "").upper(), (_LANG, _COUNTRY))

# A query is capped rather than joining every alias: Google truncates very long
# queries, and a long OR-chain drifts off the client and pulls in noise. The
# name plus a couple of aliases is what actually identifies a company.
_MAX_TERMS = 3

# The field the themes must sit in. Two is enough to say "fashion retail" or
# "DeFi liquidity" without the AND-clause growing long enough to exclude
# everything: each extra term widens the scope again, which is the opposite of
# what it is for.
_MAX_CONTEXT_TERMS = 2

# How many themes the radar may search at once. Higher than the name search
# because the two queries fail differently: three terms is what identifies a
# company, while a mandate's themes are a list of a dozen and the ones past the
# cap were simply never searched — an operator could add a theme and see nothing
# change, forever.
#
# The number depends on whether the query is constrained, because measurement
# says the field clause is what prevents drift, not the cap. Against live feeds
# for a fashion retailer: three themes AND "Fashion" returned 6 on-topic hits,
# six themes AND "Fashion" returned 11 on-topic hits, and the same six themes
# with no field returned 100 — Rhine water levels and French logistics among
# them. So: a scoped query may be long, an unscoped one may not.
_MAX_TOPIC_TERMS_SCOPED = 6
_MAX_TOPIC_TERMS_UNSCOPED = 3

# Feeds are labelled so a run's logs and the settings view make it obvious which
# results came from a search rather than a subscribed publication.
_FEED_LABEL = "Google News: {client}"

# The topic radar is labelled distinctly because its results are a different kind
# of thing: market coverage the client can speak to, not coverage *of* the client.
_TOPIC_FEED_LABEL = "Themen-Radar: {client}"


def query_url(
    terms: list[str],
    *,
    lang: str = _LANG,
    country: str = _COUNTRY,
    context: list[str] | None = None,
    max_terms: int | None = None,
) -> str:
    """Build the Google News RSS search URL for ``terms``.

    Terms are quoted and OR-joined so ``Deutsche Bahn`` matches as a phrase
    rather than as two loose words, which would return every story containing
    "deutsche".

    ``context`` narrows the result to a field: the terms must appear *and* one of
    the context terms must too. Without it the topic radar searched a bare
    OR-chain of whatever an operator typed, and single common words are the ones
    they type — a mandate whose themes included "Wachstum" and "Mode" got Xbox's
    growth figures, Canada's GDP and the Apple Watch, then a drafting model that
    correctly refused to build a statement out of them. The client's own industry
    is the missing half of the question: not "who wrote about growth" but "who
    wrote about growth *in this field*".

    ``max_terms`` overrides how many terms survive the cap. The name search wants
    three (a name and two aliases is what identifies a company); the topic radar
    wants more, because a mandate's themes are a list of a dozen and the ones past
    the cap were never searched at all.
    """
    cleaned = [t.strip() for t in terms if t and t.strip()]
    if not cleaned:
        raise ValueError("at least one search term is required")
    query = " OR ".join(f'"{term}"' for term in cleaned[: max_terms or _MAX_TERMS])
    scope = [t.strip() for t in (context or []) if t and t.strip()][:_MAX_CONTEXT_TERMS]
    if scope:
        field = " OR ".join(f'"{term}"' for term in scope)
        query = f"({query}) AND ({field})"
    params = {
        "q": query,
        "hl": lang,
        "gl": country,
        "ceid": f"{country}:{lang}",
    }
    return f"{_SEARCH_URL}?{urllib.parse.urlencode(params)}"


def client_terms(client: Client) -> list[str]:
    """The search terms identifying one client: its name, then its aliases.

    Each is stripped of its legal form (:mod:`newspulse.company_names`): the
    terms are quoted as phrases, and no headline writes "GmbH", so searching for
    it can only exclude the coverage worth finding. Unlike the matcher, which
    keeps both variants, the query keeps only the stripped one — it has three
    slots, and spending one on a phrase nothing matches wastes it.

    Duplicates are dropped case-insensitively so an alias that merely restates
    the name — "Zalando SE" beside "Zalando" — does not consume a slot.
    """
    terms: list[str] = []
    seen: set[str] = set()
    for raw in [client.name, *client.aliases]:
        term = company_names.strip_legal_form((raw or "").strip())
        if not term:
            continue
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        terms.append(term)
    return terms


def client_feeds(clients: list[Client]) -> list[Feed]:
    """One search feed per client, ready for the normal fetch pipeline.

    A client with no usable name yields no feed rather than a query that would
    match everything.
    """
    feeds: list[Feed] = []
    for client in clients:
        terms = client_terms(client)
        if not terms:
            continue
        lang, country = edition_for(client)
        feeds.append(
            Feed(
                name=_FEED_LABEL.format(client=client.name),
                url=query_url(terms, lang=lang, country=country),
                industry=client.industry,
                per_entry_source=True,
            )
        )
    return feeds


def topic_terms(client: Client) -> list[str]:
    """The client's own themes: its keywords first, then its alert topics.

    Both, because they are the same thing from two angles — what this mandate is
    about, and what it must not miss — and an operator who filled in one rarely
    filled in the other. Keywords lead: they describe the mandate's field, while
    an alert topic is usually the sharper, rarer event inside it. The order is
    therefore also a priority: :func:`query_url` keeps only the first few, because
    a long OR-chain drifts off topic and returns a general news feed.
    """
    terms: list[str] = []
    seen: set[str] = set()
    for raw in [*(client.keywords or []), *(client.alert_topics or [])]:
        term = (raw or "").strip()
        if not term:
            continue
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        terms.append(term)
    return terms


def context_terms(client: Client) -> list[str]:
    """The field the client's themes have to sit in, from its industry label.

    Operators write the industry as a short list — "Onchain-Liquidität; DeFi",
    "E-Commerce Handel" — so it is split on the separators they actually use and
    each part kept as a phrase. A mandate with no industry gets no constraint;
    that is the honest fallback, since inventing a field would silently change
    what the radar finds.
    """
    raw = (getattr(client, "industry", "") or "").strip()
    if not raw:
        return []
    terms: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[;,/]|\band\b|\bund\b", raw):
        term = part.strip(" .-")
        if not term:
            continue
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        terms.append(term)
    return terms


def same_field(one: Client, other: Client) -> bool:
    """Whether two companies name the same field.

    Used to group the competitor picker. Share of voice is a statement about *a
    market*: every monitored competitor used to be offered to every mandate, so a
    finance platform was invited to benchmark itself against fashion brands that
    exist in the portfolio only because a fashion mandate needed them.
    """
    field = {t.casefold() for t in context_terms(one)}
    return bool(field & {t.casefold() for t in context_terms(other)})


def unscoped_topic_url(client: Client) -> str | None:
    """The same radar query without its field constraint, or ``None``.

    ``None`` when the client has no themes (there is no query to widen) or no
    industry (the query was never narrowed). The caller uses this only when the
    scoped query came back empty: an AND between a narrow theme phrase and a
    narrow industry label can intersect to nothing, and a radar that goes dark is
    worse than one that returns a few loosely related stories the drafting model
    is free to refuse.
    """
    terms = topic_terms(client)
    if not terms or not context_terms(client):
        return None
    lang, country = edition_for(client)
    # Shorter than the scoped query on purpose: without the field clause a long
    # OR-chain is exactly what drifts (measured: six unscoped themes returned a
    # hundred items, among them Rhine water levels for a fashion retailer).
    return query_url(
        terms, lang=lang, country=country, max_terms=_MAX_TOPIC_TERMS_UNSCOPED
    )


def topic_feeds(clients: list[Client]) -> dict[int, Feed]:
    """One topic-radar feed per client that has themes, keyed by client id.

    Keyed rather than listed because provenance is the whole point here: an item
    from this feed is an *opening* for that one client, and nothing in the item's
    text will say so — the client is not mentioned in it. The caller therefore has
    to keep the pairing it fetched with (see ``job._fetch_topics``), which a flat
    list of feeds would throw away.

    A client with no keywords and no alert topics gets no radar. That is the
    honest default: without themes there is no way to tell which market coverage
    concerns them, and searching their industry label alone would return a general
    news feed to pitch from.
    """
    feeds: dict[int, Feed] = {}
    for client in clients:
        terms = topic_terms(client)
        if not terms:
            continue
        lang, country = edition_for(client)
        field = context_terms(client)
        feeds[client.id] = Feed(
            name=_TOPIC_FEED_LABEL.format(client=client.name),
            url=query_url(
                terms,
                lang=lang,
                country=country,
                context=field,
                max_terms=(
                    _MAX_TOPIC_TERMS_SCOPED if field else _MAX_TOPIC_TERMS_UNSCOPED
                ),
            ),
            industry=client.industry,
            per_entry_source=True,
        )
    return feeds


__all__ = [
    "client_feeds",
    "edition_for",
    "client_terms",
    "query_url",
    "topic_feeds",
    "topic_terms",
]
