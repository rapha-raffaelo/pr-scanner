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

import urllib.parse

from .feeds import Feed
from .models import Client

# The public Google News RSS search endpoint.
_SEARCH_URL = "https://news.google.com/rss/search"

# German-language, German edition. `ceid` is the country:language pair Google
# uses to pick an edition; it must agree with hl/gl or the edition silently
# falls back to US English.
_LANG = "de"
_COUNTRY = "DE"

# A query is capped rather than joining every alias: Google truncates very long
# queries, and a long OR-chain drifts off the client and pulls in noise. The
# name plus a couple of aliases is what actually identifies a company.
_MAX_TERMS = 3

# Feeds are labelled so a run's logs and the settings view make it obvious which
# results came from a search rather than a subscribed publication.
_FEED_LABEL = "Google News: {client}"


def query_url(terms: list[str], *, lang: str = _LANG, country: str = _COUNTRY) -> str:
    """Build the Google News RSS search URL for ``terms``.

    Terms are quoted and OR-joined so ``Deutsche Bahn`` matches as a phrase
    rather than as two loose words, which would return every story containing
    "deutsche".
    """
    cleaned = [t.strip() for t in terms if t and t.strip()]
    if not cleaned:
        raise ValueError("at least one search term is required")
    query = " OR ".join(f'"{term}"' for term in cleaned[:_MAX_TERMS])
    params = {
        "q": query,
        "hl": lang,
        "gl": country,
        "ceid": f"{country}:{lang}",
    }
    return f"{_SEARCH_URL}?{urllib.parse.urlencode(params)}"


def client_terms(client: Client) -> list[str]:
    """The search terms identifying one client: its name, then its aliases.

    Duplicates are dropped case-insensitively so an alias that merely restates
    the name does not consume one of the few term slots.
    """
    terms: list[str] = []
    seen: set[str] = set()
    for raw in [client.name, *client.aliases]:
        term = (raw or "").strip()
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
        feeds.append(
            Feed(
                name=_FEED_LABEL.format(client=client.name),
                url=query_url(terms),
                industry=client.industry,
                per_entry_source=True,
            )
        )
    return feeds


__all__ = ["client_feeds", "client_terms", "query_url"]
