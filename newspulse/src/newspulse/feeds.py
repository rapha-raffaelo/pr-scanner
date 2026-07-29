"""The curated feed registry.

The list of German feeds ships as an editable TOML file (``feeds_default.toml``)
that lives next to this module, so an operator can add, remove, or retag a feed
without touching Python. This module only reads and validates that file into a
list of :class:`Feed` records; it never fetches anything (that is ``ingest.py``).
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

_log = logging.getLogger(__name__)

# The default registry filename, resolved as package data so it is found whether
# NewsPulse runs from a source checkout or an installed wheel.
_DEFAULT_FEEDS_FILENAME = "feeds_default.toml"

# Top-level key in the TOML holding the array of feed tables.
_FEEDS_KEY = "feeds"


@dataclass(frozen=True, slots=True)
class Feed:
    """One registered feed. ``industry`` is an optional coarse sector tag used to
    label trade-press feeds; national general outlets leave it ``None``."""

    name: str
    url: str
    industry: str | None = None
    # True for an aggregator feed whose entries each name their own publisher
    # (see newspulse.gnews). Not settable from the registry TOML: every entry
    # there is a publisher's own feed, where the registry name *is* the outlet.
    per_entry_source: bool = False


def _coerce_industry(raw: object) -> str | None:
    """Normalize the optional industry tag: an empty or whitespace-only value in
    the TOML means "no tag" rather than an empty string."""
    if not isinstance(raw, str):
        return None
    stripped = raw.strip()
    return stripped or None


def _parse_feeds(data: str, *, origin: str) -> list[Feed]:
    """Parse TOML text into ``Feed`` records, skipping malformed entries.

    A single entry missing a required ``name``/``url`` is logged and dropped so a
    typo in one row never blocks loading the whole registry.
    """
    parsed = tomllib.loads(data)
    feeds: list[Feed] = []
    for index, entry in enumerate(parsed.get(_FEEDS_KEY, [])):
        name = entry.get("name")
        url = entry.get("url")
        if not name or not url:
            _log.warning(
                "Skipping feed #%d in %s: missing name or url (%r)",
                index,
                origin,
                entry,
            )
            continue
        feeds.append(Feed(name=name, url=url, industry=_coerce_industry(entry.get("industry"))))
    return feeds


def load_feeds(path: str | Path | None = None) -> list[Feed]:
    """Load the feed registry.

    With no ``path`` the packaged ``feeds_default.toml`` is used; pass a path to
    load a custom registry file instead.
    """
    if path is None:
        data = (resources.files("newspulse") / _DEFAULT_FEEDS_FILENAME).read_text("utf-8")
        origin = _DEFAULT_FEEDS_FILENAME
    else:
        file_path = Path(path)
        data = file_path.read_text("utf-8")
        origin = str(file_path)
    return _parse_feeds(data, origin=origin)


__all__ = ["Feed", "load_feeds"]
