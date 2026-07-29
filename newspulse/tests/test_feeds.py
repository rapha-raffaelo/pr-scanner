"""Hygiene checks on the shipped feed registry (``feeds_default.toml``).

Structural only, and deliberately offline: the test suite must not depend on 44
external sites being up. Reachability is a maintenance concern, checked on demand
with ``newspulse check-feeds``.
"""

from __future__ import annotations

# --- Registry hygiene ----------------------------------------------------------


def test_default_registry_entries_are_well_formed_and_unique():
    """Guards the shipped registry offline: a duplicated URL silently halves a
    feed's value, and a duplicated name makes two outlets indistinguishable in
    the `source` column. Reachability is not checked here (no network in tests)
    — that is what `newspulse check-feeds` is for."""
    from newspulse.feeds import load_feeds

    feeds = load_feeds()
    assert len(feeds) >= 40, "the curated registry should not silently shrink"

    urls = [f.url for f in feeds]
    names = [f.name for f in feeds]
    assert len(set(urls)) == len(urls), "duplicate feed URL in the registry"
    assert len(set(names)) == len(names), "duplicate feed name in the registry"

    for feed in feeds:
        assert feed.name.strip(), f"feed with empty name: {feed.url}"
        assert feed.url.startswith("https://"), (
            f"{feed.name}: feeds must be https (plain http is silently "
            f"downgradable in transit): {feed.url}"
        )
