"""Outlet tiers: weighting a story by *where* it ran.

The analyzer judges how important a story is for the client. That judgement is
blind to the publication — so an automated "Die Zalando-Aktie zeigt Stabilität"
item from a share-price ticker can score the same as a staff-written piece in
the FAZ. Over a real month that skews the ranked feed and, above the alert
threshold, the alert rail too.

This module supplies the missing half: a tier per outlet, loaded from the
editable ``outlet_tiers.toml``. Tier 2 is the default, so an unlisted outlet is
never penalised for being unlisted — most of what the per-client Google News
search surfaces is legitimate regional and trade press.

**Scope, and why it is narrow.** Tier is used to break ties *within* an
importance score when ranking the day's feed. It is deliberately NOT used to
adjust the score itself, and NOT used in the alert decision. That was the
original design; measuring it against a hand-labelled month of real coverage
killed it. Financial wires publish genuine corporate news — job losses, a
regulator's reprimand — alongside their ticker filler, so demoting the outlet
dropped stories a PR manager must not miss, while promoting the national dailies
lifted their routine share-price pieces into the alert rail. At a threshold of 8
the weighted variant was worse on both axes at once: more alerts *and* less
coverage. Raising the alert threshold does that job properly; tier only decides
who sits above whom, where nothing can be lost.

:func:`effective_importance` implements the weighting and is kept for callers
that want to rank or report on it, but nothing in the alerting path uses it.
"""

from __future__ import annotations

import re
import tomllib
from functools import lru_cache
from importlib import resources

from .models import SCORE_MAX, SCORE_MIN

_TIERS_FILENAME = "outlet_tiers.toml"

# The tier applied to an outlet the table does not name. The middle tier: an
# unlisted outlet is usually regional or trade press the search legitimately
# found, not something to demote.
DEFAULT_TIER = 2

# Domain suffixes stripped before matching, so "SZ.de" and "SZ" are one outlet.
_TLD_RE = re.compile(r"\.(de|com|net|org|at|ch|eu|info|news)$")

# German umlauts folded to their ASCII digraphs so "Börse Online" and
# "Boerse Online" collapse to one key — outlets are spelled both ways in feeds.
_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalize_outlet(name: str) -> str:
    """The lookup key for an outlet name.

    Case, umlaut spelling, punctuation, whitespace and a trailing domain suffix
    are all noise for identity: feeds spell the same outlet a dozen ways.
    """
    lowered = (name or "").strip().casefold().translate(_UMLAUTS)
    return _NON_ALNUM_RE.sub("", _TLD_RE.sub("", lowered))


@lru_cache(maxsize=1)
def _tier_table() -> tuple[dict[str, int], dict[int, int]]:
    """``({normalized outlet: tier}, {tier: adjustment})`` from the packaged TOML.

    Cached: the table is packaged data that cannot change within a process.
    """
    raw = (
        resources.files("newspulse").joinpath(_TIERS_FILENAME).read_text(encoding="utf-8")
    )
    data = tomllib.loads(raw)
    by_outlet: dict[str, int] = {}
    adjustments: dict[int, int] = {DEFAULT_TIER: 0}
    for key, section in data.items():
        if not key.startswith("tier") or not isinstance(section, dict):
            continue
        tier = int(key.removeprefix("tier"))
        adjustments[tier] = int(section.get("adjustment", 0))
        for outlet in section.get("outlets", []):
            normalized = normalize_outlet(outlet)
            if normalized:
                by_outlet[normalized] = tier
    return by_outlet, adjustments


def tier_for(source: str | None) -> int:
    """The tier of ``source``; :data:`DEFAULT_TIER` when it is not listed."""
    by_outlet, _ = _tier_table()
    return by_outlet.get(normalize_outlet(source or ""), DEFAULT_TIER)


def adjustment_for(source: str | None) -> int:
    """How many points this outlet's tier adds to (or removes from) a score."""
    _, adjustments = _tier_table()
    return adjustments.get(tier_for(source), 0)


def effective_importance(importance: int, source: str | None) -> int:
    """The model's importance for this story, weighted by where it ran.

    Clamped to the model's own 0..10 range so a weighted score stays comparable
    with a raw one and can never fall outside the scale the UI renders.
    """
    return max(SCORE_MIN, min(SCORE_MAX, importance + adjustment_for(source)))


__all__ = [
    "DEFAULT_TIER",
    "adjustment_for",
    "effective_importance",
    "normalize_outlet",
    "tier_for",
]
