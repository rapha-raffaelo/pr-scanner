"""Der Pressespiegel: one period's coverage, grouped by event, ready to leave the house.

A client does not want fourteen rows about the same reprimand; they want one row
with the number fourteen on it. The grouping that turns rows into that line
already exists — :mod:`newspulse.stories` clusters wire copy of the same event —
and this module is the first place it is used in something that leaves the tool.

What a clipping may say is the complete field list of :class:`Clipping`:
headline, outlet, date, the stored summary and the tonality. There is no body
text field, here or anywhere in the archive (the no-scrape rule), so the
document could not print full text even by mistake — which is also what makes
this Pressespiegel the legally unproblematic kind.

"Reichweitenstärkstes Medium" is answered from the outlet tier table, never
estimated: tier 1 is the Leitmedien list in ``outlet_tiers.toml``, and the
strongest outlet of a story is simply its best-tiered one. Reach as a *figure*
stays unmeasurable (:data:`newspulse.reporting.FORBIDDEN_FIGURES`); naming which
of two outlets sets the agenda is a fact the tier table states, not a number
nobody could check.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import outlets, prose
from . import stories as stories_mod
from .models import Analysis, Article, Client, Tonality, visible_coverage
from .reporting import Period


@dataclass(frozen=True, slots=True)
class Clipping:
    """One piece as the document prints it — and the complete list of what it may say.

    ``importance`` is carried for ranking (the best copy of a story leads it) and
    is never printed: a client document that scored its own press would be
    grading the newspaper.
    """

    headline: str
    source: str
    published_at: dt.datetime
    summary: str
    tonality: Tonality
    url: str
    importance: int


@dataclass(frozen=True, slots=True)
class ClippingStory:
    """One event: its best headline, how far it travelled, and every piece.

    ``pickup_count`` counts distinct outlets, not rows — two feeds delivering the
    same outlet twice is not two pickups, which is the rule
    :class:`newspulse.stories.Story` already applies.
    """

    headline: str
    pickup_count: int
    #: The best-tiered outlet that ran the story; ties go to the earliest.
    top_outlet: str
    #: Chronological, so a story reads as it unfolded.
    items: tuple[Clipping, ...]


@dataclass(frozen=True, slots=True)
class PressClippings:
    """Everything the rendered Pressespiegel says, in one shape.

    One object for both the page and the export, for the same reason the report
    document is one object: "the file carries what the screen showed" should be
    true by construction rather than by two templates being kept in step.
    """

    client_id: int
    client_name: str
    period: Period
    stories: tuple[ClippingStory, ...]

    @property
    def total(self) -> int:
        """How many pieces the period holds, across all stories."""
        return sum(len(story.items) for story in self.stories)

    @property
    def period_last(self) -> dt.datetime:
        """The last day the period contains.

        ``Period.end`` is exclusive, and a header reading "1.7. bis 1.8." claims
        a day the document does not cover — the same correction the report makes.
        """
        return self.period.end - dt.timedelta(days=1)


def _top_outlet(items: Sequence[Clipping]) -> str:
    """The outlet whose coverage carries furthest, by tier.

    Distinct outlets in order of first appearance, then the best (lowest) tier
    wins; among equals the one that ran the story first. Deterministic on
    purpose: the same month must always name the same outlet.
    """
    seen = tuple(dict.fromkeys(item.source for item in items))
    return min(seen, key=lambda source: (outlets.tier_for(source), seen.index(source)))


def _rows(session: Session, client_id: int, period: Period) -> list[Clipping]:
    """The period's visible coverage as clippings, chronological.

    The same relevance gate as every other view of a client's coverage
    (:func:`newspulse.models.visible_coverage`), so a piece dismissed on screen
    cannot resurface in a document that leaves the house.
    """
    found = session.execute(
        select(Analysis, Article)
        .join(Article, Article.id == Analysis.article_id)
        .where(
            Analysis.client_id == client_id,
            visible_coverage(),
            Article.published_at >= period.start,
            Article.published_at < period.end,
        )
        .order_by(Article.published_at, Article.id)
    ).all()
    return [
        Clipping(
            headline=article.title or "",
            source=article.source or "",
            published_at=article.published_at,
            # The analysis' own summary first — the stored, client-facing one the
            # workbook already exports — with the feed snippet standing in for
            # rows analysed before summaries were written. Both are stored text;
            # nothing is fetched or generated for the document.
            summary=prose.plain(analysis.summary or "") or (article.summary_text or ""),
            tonality=analysis.tonality,
            url=article.url or "",
            importance=analysis.importance_score,
        )
        for analysis, article in found
    ]


def build(session: Session, client: Client, period: Period) -> PressClippings:
    """One mandate's period, grouped by story, heaviest story first.

    Clustering input is ranked richest copy first — :func:`stories.cluster`
    makes the first member of a group its lead — so each story is headed by its
    strongest write-up. Within a story the pieces then run chronologically,
    because a story is read as it unfolded. An empty period returns an empty
    ``stories`` tuple and nothing else: the template says what that means.
    """
    pieces = _rows(session, client.id, period)
    ranked = sorted(
        pieces, key=lambda piece: (-piece.importance, piece.published_at, piece.headline)
    )
    grouped = []
    for story in stories_mod.cluster(ranked):
        members = tuple(
            sorted(
                story.members,
                key=lambda piece: (piece.published_at, piece.source, piece.headline),
            )
        )
        grouped.append(
            ClippingStory(
                headline=story.lead.headline,
                pickup_count=story.pickup_count,
                top_outlet=_top_outlet(members),
                items=members,
            )
        )
    # The widest-travelled event first; among equals the one that broke first.
    grouped.sort(
        key=lambda story: (
            -story.pickup_count,
            -len(story.items),
            story.items[0].published_at,
            story.headline,
        )
    )
    return PressClippings(
        client_id=client.id,
        client_name=client.name,
        period=period,
        stories=tuple(grouped),
    )


__all__ = ["Clipping", "ClippingStory", "PressClippings", "build"]
