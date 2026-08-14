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

**Read the survey before applying it.** Measured on a real database, the standard
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
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .matching import on_theme, radar_matcher
from .models import Article, Client, TopicHit

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Removal:
    """One hit that does not belong, kept whole so a dry run can be read."""

    client: str
    outlet: str
    headline: str
    article_id: int
    client_id: int


def survey(session: Session) -> list[Removal]:
    """Every stored hit that carries none of its mandate's themes."""
    stale: list[Removal] = []
    for client in session.scalars(select(Client)).all():
        matcher = radar_matcher(client)
        if matcher is None:
            # No themes, so nothing to judge against — and no radar either. Rows
            # like these predate the client's themes being cleared; leave them.
            continue
        rows = session.execute(
            select(Article, TopicHit)
            .join(TopicHit, TopicHit.article_id == Article.id)
            .where(TopicHit.client_id == client.id)
        ).all()
        for article, _hit in rows:
            if on_theme(article, matcher):
                continue
            stale.append(
                Removal(
                    client=client.name,
                    outlet=article.source,
                    headline=article.title,
                    article_id=article.id,
                    client_id=client.id,
                )
            )
    return stale


def clean(session: Session, *, apply: bool = False) -> list[Removal]:
    """Survey, and delete only when asked. Returns what was (or would be) removed."""
    stale = survey(session)
    if not stale or not apply:
        return stale
    for row in stale:
        session.execute(
            delete(TopicHit).where(
                TopicHit.client_id == row.client_id,
                TopicHit.article_id == row.article_id,
            )
        )
    session.commit()
    _log.info("removed %d off-theme radar hit(s)", len(stale))
    return stale


__all__ = ["Removal", "survey", "clean"]
