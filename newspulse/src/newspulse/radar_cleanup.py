"""Remove radar hits that were never this mandate's field.

A ``topic_hits`` row says "a search for this client surfaced this article", and
for a while the search could surface anything: when the field-scoped query came
back thin the radar re-asked without the field clause, and a bare OR-chain of
themes returns whatever Google feels like. That is how Russian crypto legislation
entered a business-banking mandate's radar and how CoinDesk and Cointelegraph came
to be listed as the press that covers it.

The guard now runs at write time, but rows already stored keep their reach: they
still fill the Marktumfeld page and still count as material an impulse may be
drafted from. This removes them, on the same standard the guard applies — the
article's own headline or feed snippet has to carry one of the mandate's themes.

Read-only unless ``apply=True``. Nothing else is touched: the articles stay in the
archive, where they belong to whichever mandate's field they really are.

**Read the survey before removing anything.** Measured on a real database, the standard
is blunter than it sounds: for a mandate whose theme is "Digital Markets Act" it
flags "EU verhängt 890 Millionen Strafe gegen Google", because the coverage says
Big-Tech-Regeln and never the theme's own words. That row is arguably coverage of
the mandate rather than of its market — but "arguably" is not a basis for a
delete. Hence the survey, and hence the flag: this is a tool for a person who has
looked, not a step in the sweep.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import delete, select, tuple_
from sqlalchemy.orm import Session

from .matching import on_theme, radar_matcher
from .models import Article, Client, TopicHit

_log = logging.getLogger(__name__)

#: How far back the survey looks. The same ninety days every other radar feature
#: reads, so a hit outside it is not material for anything and deleting it buys
#: nothing but risk.
WINDOW = dt.timedelta(days=90)


@dataclass(frozen=True, slots=True)
class Removal:
    """One hit that does not belong, kept whole so a dry run can be read."""

    client: str
    outlet: str
    headline: str
    #: So the reader can open the story before deciding. A bare headline is not
    #: enough to judge whether a hit belongs to a mandate, and this module insists
    #: the list is read.
    url: str
    published_at: dt.datetime
    article_id: int
    client_id: int


def survey(session: Session, *, now: dt.datetime | None = None) -> list[Removal]:
    """Stored hits in the working window that carry none of their mandate's themes.

    Windowed, because the standard is the mandate's *current* themes and its
    history was gathered under whichever ones it had at the time. A mandate that
    was given a radar last night would otherwise have its entire archive judged
    against four terms chosen the same night. Ninety days is the window every
    other radar feature reads, so nothing outside it is material for anything.
    """
    since = (now or dt.datetime.now(dt.UTC)) - WINDOW
    stale: list[Removal] = []
    for client in session.scalars(select(Client).where(Client.active.is_(True))).all():
        matcher = radar_matcher(client)
        if matcher is None:
            # No themes, so nothing to judge against — and no radar either. Rows
            # like these predate the client's themes being cleared; leave them.
            continue
        articles = session.scalars(
            select(Article)
            .join(TopicHit, TopicHit.article_id == Article.id)
            .where(TopicHit.client_id == client.id, Article.published_at >= since)
            .order_by(Article.published_at.desc())
        ).all()
        for article in articles:
            if on_theme(article, matcher):
                continue
            stale.append(
                Removal(
                    client=client.name,
                    outlet=article.source,
                    headline=article.title,
                    url=article.url,
                    published_at=article.published_at,
                    article_id=article.id,
                    client_id=client.id,
                )
            )
    return stale


def remove(session: Session, pairs: Sequence[tuple[int, int]]) -> int:
    """Delete exactly these ``(client_id, article_id)`` links. Returns how many.

    Exactly these, and not "whatever a fresh survey finds", which is what this
    used to do. Two things were wrong with that. The page renders a bounded
    number of rows while the survey is unbounded, so an operator could read forty
    and destroy eight hundred — under a docstring insisting they read the list
    first. And the set surveyed and the set deleted were two separate queries, so
    anything the night's sweep added in between went unseen.

    One statement rather than one per row: a portfolio-wide clean is a few
    hundred links and there is no reason to pay a round trip for each.
    """
    wanted = [(int(c), int(a)) for c, a in pairs]
    if not wanted:
        return 0
    removed = session.execute(
        delete(TopicHit).where(
            tuple_(TopicHit.client_id, TopicHit.article_id).in_(wanted)
        )
    ).rowcount
    session.commit()
    _log.info("removed %d off-theme radar hit(s)", removed)
    return removed


__all__ = ["Removal", "WINDOW", "survey", "remove"]
