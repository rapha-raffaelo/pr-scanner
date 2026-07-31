"""The coverage map: which outlets write about whom, and who is missing you.

Share of voice says *how much* was written. It cannot say *where* — and where is
what a pitch list is made of. This computes the outlet-by-company grid behind
the map, and from it the question worth acting on:

    Which publications cover our competitors regularly, and never us?

Every input already exists. No new source, no model call — this is a different
question asked of the archive.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Analysis, Article, Client, visible_coverage


# An outlet that wrote about a rival once is not a pattern; the gap list is a
# pitch list, and a pitch aimed at a one-off mention wastes the relationship.
_MIN_RIVAL_ARTICLES = 2


@dataclass(frozen=True, slots=True)
class MapCell:
    """One outlet's coverage of one company."""

    company: str
    articles: int


@dataclass(frozen=True, slots=True)
class MapRow:
    """One outlet, and how it covered every company in the comparison."""

    source: str
    cells: tuple[MapCell, ...]
    client_articles: int
    total: int

    @property
    def rival_articles(self) -> int:
        """Everything this outlet wrote about the competition."""
        return self.total - self.client_articles

    @property
    def lean(self) -> int:
        """How one-sided this outlet is: positive means it favours the rivals.

        The sort key of the diverging view — the number the reader is actually
        looking for is "who writes about them and not us", and that is exactly
        this difference.
        """
        return self.rival_articles - self.client_articles

    @property
    def is_gap(self) -> bool:
        """True when this outlet covers rivals but never the client — a target."""
        return self.client_articles == 0 and self.total >= _MIN_RIVAL_ARTICLES


@dataclass(frozen=True, slots=True)
class CoverageMap:
    """The whole grid plus the companies that form its columns."""

    client: str
    companies: tuple[str, ...]
    rows: tuple[MapRow, ...]

    @property
    def widest(self) -> int:
        """The largest single-side count, for scaling the diverging bars. Never
        zero, so the template can divide unconditionally."""
        return max(
            (max(r.rival_articles, r.client_articles) for r in self.rows), default=1
        ) or 1

    @property
    def leaning(self) -> tuple[MapRow, ...]:
        """The outlets worth plotting: any with a real imbalance, biggest first.

        A 78-row grid of mostly-empty cells is unreadable — and most of those
        rows say nothing. This keeps the ones that carry a signal.
        """
        return tuple(r for r in self.rows if r.total >= _MIN_RIVAL_ARTICLES)[:24]

    @property
    def gaps(self) -> tuple[MapRow, ...]:
        """Outlets writing about the competition and not about the client."""
        return tuple(row for row in self.rows if row.is_gap)

    @property
    def max_cell(self) -> int:
        """The busiest single outlet/company pairing, for bar scaling. Never zero,
        so a template can divide by it unconditionally."""
        return max((c.articles for row in self.rows for c in row.cells), default=1) or 1

    @property
    def shared(self) -> tuple[MapRow, ...]:
        """Outlets covering the client — the existing relationships."""
        return tuple(row for row in self.rows if row.client_articles > 0)


def build(
    session: Session, client: Client, *, days: int = 90, now: dt.datetime | None = None
) -> CoverageMap:
    """The outlet grid for ``client`` and its competitors over ``days``.

    A wider window than share of voice by default: a pitch gap is a standing
    relationship, and a month is short enough that a quiet trade title looks like
    a gap when it simply had nothing to write about.
    """
    since = (now or dt.datetime.now(dt.UTC)) - dt.timedelta(days=days)
    members = [client, *client.competitors]
    names = {member.id: member.name for member in members}

    rows = session.execute(
        select(Article.source, Analysis.client_id, func.count(Analysis.id))
        .join(Analysis, Analysis.article_id == Article.id)
        .where(
            Analysis.client_id.in_(names),
            visible_coverage(),
            Article.published_at >= since,
        )
        .group_by(Article.source, Analysis.client_id)
    ).all()

    by_source: dict[str, dict[int, int]] = {}
    for source, client_id, count in rows:
        by_source.setdefault(source, {})[client_id] = count

    built = [
        MapRow(
            source=source,
            cells=tuple(
                MapCell(company=names[m.id], articles=counts.get(m.id, 0))
                for m in members
            ),
            client_articles=counts.get(client.id, 0),
            total=sum(counts.values()),
        )
        for source, counts in by_source.items()
    ]
    # Sorted by lean: the outlets most tilted towards the competition lead, which
    # is the order the question "who is missing us" is asked in. Volume breaks
    # ties so a 14-0 outranks a 2-0.
    built.sort(key=lambda r: (-r.lean, -r.total, r.source))
    return CoverageMap(
        client=client.name,
        companies=tuple(m.name for m in members),
        rows=tuple(built),
    )


__all__ = ["CoverageMap", "MapCell", "MapRow", "build"]
