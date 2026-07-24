"""Unit tests for RSS ingestion and the feed registry.

These test the two public entry points directly:

* ``ingest.fetch_feed`` — parses a fed-in feed document (the committed fixture, or
  a tiny inline one) into ``FeedItem`` records. The single HTTP boundary
  (``_fetch_raw`` / ``urllib.request.urlopen``) is always mocked, so no test
  touches the network. The fault-isolation cases (timeout, URLError, malformed)
  assert an empty return plus a WARNING rather than a raised exception.
* ``feeds.load_feeds`` — reads the packaged registry and custom TOML files.

No test drives a multi-feed sweep or the daily job; those belong to later stories.
The isolation guarantee here is exactly "``fetch_feed`` returns [] instead of
raising", which is what lets a caller's loop continue.
"""

from __future__ import annotations

import datetime as dt
import logging
import socket
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from newspulse import feeds, ingest

_FIXTURE = Path(__file__).parent / "fixtures" / "feed_sample.xml"
_FEED_URL = "https://beispiel.example.de/feed.xml"

# A cutoff that keeps the two July items in the fixture and drops the June one.
_SINCE = dt.datetime(2026, 7, 1, 0, 0, tzinfo=dt.UTC)


@pytest.fixture
def fixture_bytes() -> bytes:
    return _FIXTURE.read_bytes()


def _patch_fetch(monkeypatch, payload: bytes) -> MagicMock:
    """Replace the single HTTP boundary with a mock returning ``payload``."""
    mock = MagicMock(return_value=payload)
    monkeypatch.setattr(ingest, "_fetch_raw", mock)
    return mock


# --- fetch_feed: parsing -------------------------------------------------------


def test_fetch_feed_parses_fixture_into_items_newer_than_since(fixture_bytes, monkeypatch):
    _patch_fetch(monkeypatch, fixture_bytes)

    items = ingest.fetch_feed(_FEED_URL, since=_SINCE, source="Beispiel")

    # The June item is older than _SINCE and must be excluded.
    assert [item.title for item in items] == [
        "Beispiel AG kündigt neues Elektromodell an",
        "Muster GmbH meldet Rekordgewinn im zweiten Quartal",
    ]


def test_fetch_feed_captures_all_feed_provided_fields(fixture_bytes, monkeypatch):
    _patch_fetch(monkeypatch, fixture_bytes)

    first = ingest.fetch_feed(_FEED_URL, since=_SINCE, source="Beispiel")[0]

    assert first.title == "Beispiel AG kündigt neues Elektromodell an"
    assert first.link == "https://beispiel.example.de/wirtschaft/beispiel-ag-elektromodell"
    assert first.source == "Beispiel"
    assert first.summary == (
        "Der Automobilhersteller Beispiel AG stellt sein erstes "
        "vollelektrisches Modell vor."
    )
    assert first.language == "de"


def test_fetch_feed_normalizes_published_at_to_utc(fixture_bytes, monkeypatch):
    _patch_fetch(monkeypatch, fixture_bytes)

    first = ingest.fetch_feed(_FEED_URL, since=_SINCE, source="Beispiel")[0]

    # Fixture pubDate is 09:00 +0200 → 07:00 UTC, and must be tz-aware.
    assert first.published_at.tzinfo is not None
    assert first.published_at == dt.datetime(2026, 7, 24, 7, 0, tzinfo=dt.UTC)


def test_fetch_feed_defaults_source_to_channel_title(fixture_bytes, monkeypatch):
    """With no explicit source, items carry the feed's own channel title."""
    _patch_fetch(monkeypatch, fixture_bytes)

    items = ingest.fetch_feed(_FEED_URL, since=_SINCE)

    assert items[0].source == "Beispiel Wirtschaft"


# --- fetch_feed: the no-second-request guarantee -------------------------------


def test_fetch_feed_makes_exactly_one_http_call(fixture_bytes, monkeypatch):
    """Ingestion issues one HTTP request for the feed and never a second one for an
    article body (Leistungsschutzrecht). We patch the real urllib boundary and
    assert it is hit exactly once, with the feed URL."""
    response_cm = MagicMock()
    response_cm.__enter__.return_value.read.return_value = fixture_bytes
    urlopen = MagicMock(return_value=response_cm)
    monkeypatch.setattr(ingest.urllib.request, "urlopen", urlopen)

    items = ingest.fetch_feed(_FEED_URL, since=_SINCE, source="Beispiel")

    assert len(items) == 2  # proves we actually parsed, not short-circuited
    assert urlopen.call_count == 1
    request_arg = urlopen.call_args.args[0]
    assert request_arg.full_url == _FEED_URL


# --- fetch_feed: fault isolation -----------------------------------------------


def test_fetch_feed_returns_empty_and_warns_on_timeout(monkeypatch, caplog):
    def _raise_timeout(url, timeout):
        raise socket.timeout("timed out")

    monkeypatch.setattr(ingest, "_fetch_raw", _raise_timeout)

    with caplog.at_level(logging.WARNING, logger="newspulse.ingest"):
        items = ingest.fetch_feed(_FEED_URL, since=_SINCE)

    assert items == []
    assert any(record.levelno == logging.WARNING for record in caplog.records)


def test_fetch_feed_returns_empty_and_warns_on_unreachable(monkeypatch, caplog):
    def _raise_urlerror(url, timeout):
        raise urllib.error.URLError("name or service not known")

    monkeypatch.setattr(ingest, "_fetch_raw", _raise_urlerror)

    with caplog.at_level(logging.WARNING, logger="newspulse.ingest"):
        items = ingest.fetch_feed(_FEED_URL, since=_SINCE)

    assert items == []
    assert "could not be fetched" in caplog.text


def test_fetch_feed_returns_empty_and_warns_on_malformed(monkeypatch, caplog):
    _patch_fetch(monkeypatch, b"this is not xml at all <<< totally broken")

    with caplog.at_level(logging.WARNING, logger="newspulse.ingest"):
        items = ingest.fetch_feed(_FEED_URL, since=_SINCE)

    assert items == []
    assert "malformed" in caplog.text


def test_fetch_feed_does_not_raise_on_failure(monkeypatch):
    """The isolation contract: a failing feed returns [] rather than propagating,
    which is what keeps a multi-feed sweep going."""
    def _boom(url, timeout):
        raise urllib.error.URLError("boom")

    monkeypatch.setattr(ingest, "_fetch_raw", _boom)

    # Must not raise.
    assert ingest.fetch_feed(_FEED_URL, since=_SINCE) == []


# --- fetch_feed: dateless-item fallback ----------------------------------------


def test_fetch_feed_falls_back_to_fetched_at_when_date_missing(monkeypatch, caplog):
    """An item with no pubDate falls back to fetched_at and logs at DEBUG."""
    dateless = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<rss version="2.0"><channel><title>T</title><language>de</language>'
        b"<item><title>Ohne Datum</title>"
        b"<link>https://beispiel.example.de/ohne-datum</link>"
        b"<description>Kein pubDate.</description></item>"
        b"</channel></rss>"
    )
    _patch_fetch(monkeypatch, dateless)
    fetched_at = dt.datetime(2026, 7, 24, 10, 0, tzinfo=dt.UTC)

    with caplog.at_level(logging.DEBUG, logger="newspulse.ingest"):
        items = ingest.fetch_feed(
            _FEED_URL, since=_SINCE, source="Beispiel", fetched_at=fetched_at
        )

    assert len(items) == 1
    assert items[0].published_at == fetched_at
    assert any(record.levelno == logging.DEBUG for record in caplog.records)


def test_fetch_feed_returns_feeditem_instances(fixture_bytes, monkeypatch):
    _patch_fetch(monkeypatch, fixture_bytes)

    items = ingest.fetch_feed(_FEED_URL, since=_SINCE)

    assert all(isinstance(item, ingest.FeedItem) for item in items)


# --- feeds registry ------------------------------------------------------------

# Acceptance: the curated registry ships 30 to 60 German feeds.
_MIN_FEEDS = 30
_MAX_FEEDS = 60


def test_default_registry_has_between_30_and_60_feeds():
    registry = feeds.load_feeds()
    assert _MIN_FEEDS <= len(registry) <= _MAX_FEEDS


def test_default_registry_entries_have_name_and_url():
    for feed in feeds.load_feeds():
        assert feed.name
        assert feed.url.startswith("http")


def test_default_registry_includes_the_named_outlets():
    """The acceptance names specific outlets that must be present."""
    names = {feed.name for feed in feeds.load_feeds()}
    for expected in ("Spiegel", "Zeit", "Handelsblatt", "Welt", "n-tv", "Tagesschau"):
        assert expected in names


def test_industry_tag_is_optional():
    """At least one feed is untagged (None) and at least one carries a tag."""
    registry = feeds.load_feeds()
    assert any(feed.industry is None for feed in registry)
    assert any(feed.industry for feed in registry)


def test_load_feeds_reads_a_custom_path(tmp_path):
    custom = tmp_path / "custom.toml"
    custom.write_text(
        '[[feeds]]\nname = "Nur Ein Feed"\nurl = "https://example.de/rss"\n'
        'industry = "  "\n',
        encoding="utf-8",
    )

    registry = feeds.load_feeds(custom)

    assert len(registry) == 1
    assert registry[0] == feeds.Feed(name="Nur Ein Feed", url="https://example.de/rss")
    # Whitespace-only industry coerces to None.
    assert registry[0].industry is None


def test_load_feeds_skips_entries_missing_required_fields(tmp_path, caplog):
    custom = tmp_path / "custom.toml"
    custom.write_text(
        '[[feeds]]\nname = "Gut"\nurl = "https://ok.de/rss"\n'
        '[[feeds]]\nname = "Ohne URL"\n',
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="newspulse.feeds"):
        registry = feeds.load_feeds(custom)

    assert [feed.name for feed in registry] == ["Gut"]
    assert "missing name or url" in caplog.text
