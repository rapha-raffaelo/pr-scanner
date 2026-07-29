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
    """An alias restating the name must not consume one of the few term slots."""
    c = _client("Siemens AG", aliases=["siemens ag", "Siemens", "  "])
    assert gnews.client_terms(c) == ["Siemens AG", "Siemens"]


def test_client_feeds_builds_one_aggregator_feed_per_client():
    feeds = gnews.client_feeds([_client("Siemens AG", ["Siemens"], industry="Industrie")])
    assert len(feeds) == 1
    feed = feeds[0]
    assert "Siemens AG" in feed.name
    assert feed.industry == "Industrie"
    # The decisive flag: entries carry their own publisher, not this feed's name.
    assert feed.per_entry_source is True
    assert _q(feed.url) == '"Siemens AG" OR "Siemens"'


def test_client_without_a_usable_name_yields_no_feed():
    """A blank name would build a query that matches everything."""
    assert gnews.client_feeds([_client("   ")]) == []
