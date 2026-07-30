"""Per-client Google News query feeds (newspulse.gnews).

Offline: query construction and the client→feed mapping are pure functions, and
the item-normalization quirks this source introduces are covered in test_ingest
against fixture bytes. Reachability is not a unit-test concern.
"""

from __future__ import annotations

import urllib.parse

import pytest

from newspulse import gnews
from newspulse.models import Client


def _client(name: str, aliases: list[str] | None = None, industry: str | None = None) -> Client:
    return Client(
        name=name,
        aliases=aliases or [],
        industry=industry,
        country="DE",
        keywords=[],
        alert_topics=[],
    )


def _q(url: str) -> str:
    """The decoded `q` parameter of a query URL."""
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["q"][0]


def test_query_quotes_each_term_so_multiword_names_match_as_phrases():
    """Unquoted, "Deutsche Bahn" would match every story containing "deutsche"."""
    assert _q(gnews.query_url(["Deutsche Bahn"])) == '"Deutsche Bahn"'


def test_query_or_joins_multiple_terms():
    assert _q(gnews.query_url(["Siemens AG", "Siemens"])) == '"Siemens AG" OR "Siemens"'


def test_query_targets_the_german_edition():
    """hl/gl/ceid must agree or Google silently serves the US English edition."""
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(gnews.query_url(["X"])).query)
    assert params["hl"] == ["de"]
    assert params["gl"] == ["DE"]
    assert params["ceid"] == ["DE:de"]


def test_query_caps_the_number_of_terms():
    """A long OR-chain drifts off the client and Google truncates it anyway."""
    url = gnews.query_url(["a", "b", "c", "d", "e"])
    assert _q(url).count(" OR ") == 2  # three terms => two joins


def test_query_requires_at_least_one_usable_term():
    with pytest.raises(ValueError):
        gnews.query_url([])
    with pytest.raises(ValueError):
        gnews.query_url(["   ", ""])


def test_client_terms_are_name_then_aliases_deduplicated_case_insensitively():
    """An alias restating the name must not consume one of the few term slots.

    With the legal form dropped, "Siemens AG", "siemens ag" and "Siemens" all
    reduce to the same term — which is the point: three slots, one company.
    """
    c = _client("Siemens AG", aliases=["siemens ag", "Siemens", "  "])
    assert gnews.client_terms(c) == ["Siemens"]


def test_client_terms_drop_the_legal_form_from_the_phrase_query():
    """Regression: a name entered with its legal form found nothing.

    The terms are quoted as phrases and no headline writes "GmbH", so searching
    for it spends a slot on a phrase that cannot match. The real case: coverage of
    "IB-7 Beauty Tech GmbH" is filed under "IB-7 Beauty Tech".
    """
    c = _client("IB-7 Beauty Tech GmbH", aliases=["IB-7"])
    assert gnews.client_terms(c) == ["IB-7 Beauty Tech", "IB-7"]


def test_client_terms_keep_a_name_that_is_only_a_legal_form():
    """Stripping must never leave a client with no term at all."""
    assert gnews.client_terms(_client("AG")) == ["AG"]


def test_client_feeds_builds_one_aggregator_feed_per_client():
    feeds = gnews.client_feeds([_client("Siemens AG", ["Siemens"], industry="Industrie")])
    assert len(feeds) == 1
    feed = feeds[0]
    assert "Siemens AG" in feed.name
    assert feed.industry == "Industrie"
    # The decisive flag: entries carry their own publisher, not this feed's name.
    assert feed.per_entry_source is True
    assert _q(feed.url) == '"Siemens"'


def test_client_without_a_usable_name_yields_no_feed():
    """A blank name would build a query that matches everything."""
    assert gnews.client_feeds([_client("   ")]) == []


# --- The topic radar -----------------------------------------------------------
#
# A second search per client, on its themes rather than its name. It exists for the
# coverage a mandate is *not* in: the market development it could speak to, which by
# definition never carries its name (see newspulse.angles).


def _themed(name: str, keywords: list[str], alert_topics: list[str] | None = None) -> Client:
    client = Client(
        name=name,
        aliases=[],
        industry="Krypto",
        country="DE",
        keywords=keywords,
        alert_topics=alert_topics or [],
    )
    # topic_feeds keys on the id, which an unsaved ORM object does not have.
    client.id = abs(hash(name)) % 10_000
    return client


def test_topic_terms_take_keywords_first_then_alert_topics():
    """Keywords describe the field; an alert topic is the sharper event inside it."""
    client = _themed("Arrakis", ["Onchain-Liquidität"], ["Börsenschließung"])
    assert gnews.topic_terms(client) == ["Onchain-Liquidität", "Börsenschließung"]


def test_topic_terms_deduplicate_and_drop_blanks():
    client = _themed("Arrakis", ["Liquidität", "  ", "liquidität"], ["Liquidität"])
    assert gnews.topic_terms(client) == ["Liquidität"]


def test_a_long_theme_list_is_capped_in_the_query_in_theme_order():
    """A long OR-chain drifts off topic and returns a general news feed.

    The cap lives in query_url, as it does for the name search, so ``topic_terms``
    stays a plain ordered list — and that order decides which themes survive it:
    keywords before alert topics.
    """
    client = _themed(
        "Arrakis", ["eins", "zwei", "drei", "vier"], alert_topics=["fünf"]
    )

    assert gnews.topic_terms(client) == ["eins", "zwei", "drei", "vier", "fünf"]
    query = _q(gnews.topic_feeds([client])[client.id].url)
    assert query == '"eins" OR "zwei" OR "drei"'


def test_topic_feeds_are_keyed_by_client_so_provenance_survives():
    """The pairing is the whole point: nothing in the item's text names the client."""
    client = _themed("Arrakis", ["Onchain-Liquidität"])

    feeds = gnews.topic_feeds([client])

    assert list(feeds) == [client.id]
    feed = feeds[client.id]
    assert "Themen-Radar" in feed.name
    assert "Arrakis" in feed.name
    assert feed.per_entry_source is True
    assert _q(feed.url) == '"Onchain-Liquidität"'
    # And emphatically not the client's own name: that search already exists, and
    # duplicating it here would spend a second feed on the same results.
    assert "Arrakis" not in _q(feed.url)


def test_a_client_without_themes_gets_no_radar():
    """Searching an industry label alone would return a general news feed to pitch
    from, which is worse than admitting there is nothing to watch."""
    assert gnews.topic_feeds([_themed("Arrakis", [], [])]) == {}
