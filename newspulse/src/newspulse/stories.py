"""Grouping coverage into *stories*: one event, however many outlets ran it.

A PR manager does not think in articles. When a regulator reprimands a client,
that is one story — and the number that matters is how far it travelled. Today
the archive holds it once per outlet, which reads as three separate alerts
crowding a rail that only has room for the three things that matter.

This module groups articles reporting the same event and reports the pickup
count, turning that duplication into the most useful line you can put in front
of a client: *"Bafin rügt Zalando — aufgegriffen von 14 Outlets"*.

Why this is not deduplication
-----------------------------
It would be tempting to fix this at ingest by collapsing the copies. That would
destroy the very information the feature exists to report: with one row stored
you can no longer say how many outlets ran it, or which. So the copies are kept,
and grouping happens on read. Dedup stays deliberately conservative (identical
URL, or an identical normalized headline) — it removes *the same article fetched
twice*; this removes *the same event reported many times*, which is a different
question with a different tolerance for error.

Conservatism
------------
Two stories wrongly merged is a story a human never sees, which is the one
failure this tool cannot recover from — the same reasoning that gates dedup's
title collapse. So the similarity bar is high, thin headlines are never grouped,
and a differently-angled write-up ("Harte Tage für Zalando: Bafin rügt
Geschäftsbericht – Aktie gibt nach") stays its own story even though it concerns
the same event. Under-grouping merely shows a duplicate; over-grouping hides news.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, Sequence, TypeVar

from . import outlets

# How alike two headlines must be, as a Jaccard overlap of their significant
# tokens, before they are treated as the same story. Measured against real dpa
# wire copy carrying different section labels ("Online-Händler: …", "… -
# Wirtschaft - SZ.de"), which lands around 0.67-0.89; distinct write-ups about
# the same event land near 0.25. 0.6 separates them with room on both sides.
_SIMILARITY_THRESHOLD = 0.6

# A headline with fewer significant tokens than this is not grouped: too little
# signal to trust, and a short generic label ("Zalando Rückruf") is exactly the
# case where two unrelated stories look alike. Mirrors dedup's own word gate.
_MIN_TOKENS = 4

_TOKEN_RE = re.compile(r"[0-9a-zà-ÿ]+")

# High-frequency German function words carry no topical signal, and leaving them
# in inflates the overlap of any two German sentences.
_STOPWORDS = frozenset(
    """
    der die das den dem des ein eine einer eines einem einen und oder aber
    auch noch nur schon wie was wer wo wann warum ist sind war waren wird
    werden wurde wurden hat habe haben hatte hatten kann können soll sollen
    muss müssen darf dürfen für von vom zu zur zum mit nach bei aus auf an
    in im ins über unter vor hinter neben zwischen durch gegen ohne um bis
    seit während wegen trotz statt sich sein seine seiner ihren ihre ihr
    als am ab es er sie wir ihr man dass ob so mehr sehr nicht kein keine
    """.split()
)


class _Rankable(Protocol):
    """The shape a story member must have: a headline, an outlet, a rank."""

    headline: str
    source: str
    importance: int


T = TypeVar("T", bound=_Rankable)


@dataclass(frozen=True, slots=True)
class Story:
    """One event and every article that reported it, richest copy first."""

    lead: object
    members: tuple
    outlets: tuple[str, ...]

    @property
    def pickup_count(self) -> int:
        """How many distinct outlets ran this story.

        Distinct by :func:`newspulse.outlets.normalize_outlet`, so one masthead
        the feeds spell two ways is one pickup.
        """
        return len(self.outlets)

    @property
    def is_syndicated(self) -> bool:
        """True when more than one outlet ran it — i.e. worth a pickup label."""
        return self.pickup_count > 1


def _tokens(headline: str, source: str) -> frozenset[str]:
    """The significant tokens of a headline, with the outlet's own name removed.

    Subtracting the source tokens strips the trailing byline ("… - Baden Online")
    without needing to know the separator conventions of every German outlet, and
    incidentally removes the outlet name wherever else it appears.
    """
    words = frozenset(_TOKEN_RE.findall((headline or "").casefold()))
    outlet = frozenset(_TOKEN_RE.findall((source or "").casefold()))
    return frozenset(w for w in words - outlet - _STOPWORDS if len(w) > 2)


def _similar(a: frozenset[str], b: frozenset[str]) -> bool:
    """Jaccard overlap of two token sets, gated on both being substantial."""
    if len(a) < _MIN_TOKENS or len(b) < _MIN_TOKENS:
        return False
    union = a | b
    if not union:
        return False
    return len(a & b) / len(union) >= _SIMILARITY_THRESHOLD


def cluster(items: Sequence[T]) -> list[Story]:
    """Group ``items`` into stories, preserving the order they arrived in.

    Single-linkage: an item joins the first existing story any of whose members
    it resembles. Input order is the caller's ranking, so the first member of a
    story is its best copy and becomes the lead — the story then sorts exactly
    where its strongest article would have.

    O(n·members) comparisons. A day's coverage is tens of items, so this is not
    worth indexing.
    """
    groups: list[list[T]] = []
    fingerprints: list[list[frozenset[str]]] = []

    for item in items:
        tokens = _tokens(item.headline, item.source)
        placed = False
        for index, prints in enumerate(fingerprints):
            if any(_similar(tokens, other) for other in prints):
                groups[index].append(item)
                prints.append(tokens)
                placed = True
                break
        if not placed:
            groups.append([item])
            fingerprints.append([tokens])

    return [
        Story(
            lead=group[0],
            members=tuple(group),
            # Ordered by first appearance and de-duplicated on the *normalized*
            # name: two feeds can deliver the same outlet twice, and they rarely
            # spell it the same way twice, so "Handelsblatt" and
            # "handelsblatt.de" are one pickup and not two.
            outlets=outlets.distinct_outlets(member.source for member in group),
        )
        for group in groups
    ]


def origin(story: Story):
    """The story's earliest member — the article that had it first.

    Everything after it is a pickup, and the distinction is what the fast lane
    turns on: a story whose first piece is four hours old and just gained its
    third outlet is still rising, one whose first piece ran yesterday is
    through. Members must carry a ``published_at``; the clusterer's own protocol
    does not require one, so this is asked only of callers who need an origin.

    Ties in the timestamp are broken by retrieval order, never by chance:
    ``min`` is stable and ``members`` preserves the order the items arrived in,
    so two pieces stamped to the same minute resolve to whichever was fetched
    first — the same answer on every run over the same rows.
    """
    return min(story.members, key=lambda member: member.published_at)


__all__ = ["Story", "cluster", "origin"]
