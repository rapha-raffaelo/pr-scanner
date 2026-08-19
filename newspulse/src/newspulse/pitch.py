"""Where to send a draft: the journalists and outlets already on the subject.

A positioning is only worth writing if somebody receives it, and the tool already
knows more about that than the consultant can hold in their head. Three things it
can say without guessing, in descending order of how much they are worth:

1. **Who wrote the very stories this draft answers.** They are demonstrably on
   the subject this week. Nothing else comes close as a signal.
2. **Who else covers this client's field** — authors on the radar's material over
   the window, whether or not they have written about the mandate.
3. **Which outlets cover the field and never the client.** No name attached, but
   it is the pitch list the coverage map already computes.

Every entry carries the headline it came from, so a recommendation can be checked
rather than trusted — the same rule the drafts themselves follow.

**What this module will not do.** It never invents a name, never guesses an email
address, and never derives one from a pattern. Feeds carry an author field
sometimes and a contact never; measured on a real archive, 22 of 291 articles had
an author at all and the Google News items had none. A plausible-looking
``vorname.nachname@medium.de`` is worse than an empty field, because it will be
used, and it will reach the wrong person or nobody. Contacts belong in a contact
book a human fills in.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import contacts as contactbook
from .matching import on_theme, radar_matcher
from .models import (
    Analysis,
    Angle,
    Article,
    Client,
    Outreach,
    TopicHit,
    visible_coverage,
)
from .outlets import tier_for

# How far back the field's journalists are read. The impulse window, so the people
# named are the ones writing about it now rather than a year ago.
LOOKBACK_DAYS = 90

# Kept short on purpose. A pitch list of twenty is a research project; the value
# here is "these four, and here is why".
MAX_TARGETS = 6

# How long a released letter keeps marking its recipient on this angle's list. A
# journalist pitched on the same subject within a quarter remembers it, and the
# second identical approach is the one that costs the relationship. The same span
# the list itself reads (LOOKBACK_DAYS), so the mark and the material age out
# together.
ALREADY_PITCHED_DAYS = 90

# An outlet that wrote about the field once is not a beat. Two is the same floor
# the coverage map uses for its gap list, for the same reason.
_MIN_FIELD_ARTICLES = 2


@dataclass(frozen=True, slots=True)
class PitchTarget:
    """One suggested recipient, with the evidence that suggested them."""

    outlet: str
    #: ``None`` when the feed carried no byline — most of the time. Named as such
    #: rather than filled with a guess.
    journalist: str | None
    #: Why this one, in the reader's language.
    reason: str
    #: Headlines behind the suggestion, so it can be checked.
    evidence: tuple[str, ...]
    #: How often they have written about this client. Zero is the interesting
    #: case: the outlet covers the field and has never mentioned the mandate.
    about_client: int
    #: The contact-book entry for this byline, if the consultant has recorded one.
    #: Never derived — see the module docstring — only looked up.
    contact_id: int | None = None
    contact_email: str = ""
    #: When a *released* letter for this same angle last went to this recipient,
    #: within :data:`ALREADY_PITCHED_DAYS`. ``None`` when none did. The mark that
    #: stops the list proposing someone who was already written to about the
    #: same thing last week.
    already_pitched_at: dt.datetime | None = None

    @property
    def is_new_contact(self) -> bool:
        return self.about_client == 0


def _authors_of(session: Session, article_ids: list[int]) -> list[tuple[str, str, str]]:
    """(author, outlet, headline) for the stories a draft rests on."""
    if not article_ids:
        return []
    rows = session.execute(
        select(Article.author, Article.source, Article.title)
        .where(Article.id.in_(article_ids), Article.author.is_not(None))
        .order_by(Article.published_at.desc())
    ).all()
    return [(a.strip(), s, t) for a, s, t in rows if (a or "").strip()]


def _radar_articles(session: Session, client: Client, since: dt.datetime) -> list[Article]:
    """This mandate's radar hits that actually read like its field.

    A ``topic_hits`` row says "a search for this client surfaced this article",
    and for a while the search was capable of surfacing anything: an unanchored
    query put Russian crypto legislation into a business-banking mandate's radar,
    and the pitch list then offered CoinDesk, Cointelegraph and Decrypt as the
    people who cover Qonto's field. "Das passt nicht" — no, and the damage is not
    cosmetic here: this list is the address book for a letter that actually gets
    sent, and a Bitcoin reporter emailed about business banking remembers it.

    The radar guard now filters at write time, but rows already stored keep their
    reach. So a recipient has to prove the article carries one of *this* mandate's
    themes before being offered. Deliberately applied here and not to the market
    material itself: a drafting model can weigh a marginal story and refuse, while
    an address list cannot.
    """
    matcher = radar_matcher(client)
    articles = session.scalars(
        select(Article)
        .join(TopicHit, TopicHit.article_id == Article.id)
        .where(TopicHit.client_id == client.id, Article.published_at >= since)
    ).all()
    if matcher is None:
        return list(articles)
    return [article for article in articles if on_theme(article, matcher)]


def _field_authors(
    session: Session, client: Client, since: dt.datetime
) -> list[tuple[str, str, int, str]]:
    """(author, outlet, articles, one headline) for people covering the field."""
    counts: dict[tuple[str, str], list] = {}
    for article in _radar_articles(session, client, since):
        author = (article.author or "").strip()
        if not author:
            continue
        key = (author, article.source)
        row = counts.setdefault(key, [0, article.title])
        row[0] += 1
    return [
        (author, source, n, title)
        for (author, source), (n, title) in sorted(
            counts.items(), key=lambda kv: -kv[1][0]
        )
    ]


def _covered_client(session: Session, client: Client, since: dt.datetime) -> dict[str, int]:
    """How often each outlet wrote about the client — the "already a contact" test."""
    rows = session.execute(
        select(Article.source, func.count(Article.id))
        .join(Analysis, Analysis.article_id == Article.id)
        .where(
            Analysis.client_id == client.id,
            visible_coverage(),
            Article.published_at >= since,
        )
        .group_by(Article.source)
    ).all()
    return {source: count for source, count in rows}


def _field_outlets(
    session: Session, client: Client, since: dt.datetime
) -> list[tuple[str, int, str]]:
    """(outlet, articles, one headline) for publications covering the field.

    Two articles is the floor, and beneath it lies the failure this fallback
    exists for. Measured on a mandate's first day: the radar found 16 market
    items across **16 different outlets, one each** — Handelsblatt, SZ, ZDFheute,
    Der Standard, Golem among them — and the floor removed every one, so the
    pitch list was empty on a day when the answer was obvious to any human
    looking at it. A rule that assumes density is right in steady state and wrong
    at exactly the moment a new mandate is being set up.

    So when the strict list is empty, the single-hit outlets are offered instead,
    best masthead first (:func:`newspulse.outlets.tier_for`) rather than in
    whatever order SQLite returns them. The count travels with each row, so
    "1 Meldung" is on screen and the reader can weigh it — the list never
    pretends a single hit is a beat.
    """
    counts: dict[str, list] = {}
    for article in _radar_articles(session, client, since):
        row = counts.setdefault(article.source, [0, article.title])
        row[0] += 1
    rows = [
        (source, n, title)
        for source, (n, title) in sorted(counts.items(), key=lambda kv: -kv[1][0])
    ]
    established = [(s, n, t) for s, n, t in rows if n >= _MIN_FIELD_ARTICLES]
    if established:
        return established
    return sorted(
        ((s, n, t) for s, n, t in rows),
        key=lambda row: (tier_for(row[0]), row[0].casefold()),
    )


def _already_pitched(
    session: Session, angle: Angle | None, reference: dt.datetime
) -> dict[tuple[str, str], dt.datetime]:
    """(journalist, outlet) → when a released letter for this angle last went to
    them, within :data:`ALREADY_PITCHED_DAYS`.

    Only released letters count: a draft reached nobody, and marking one would
    claim a contact that never happened. Keyed case-insensitively the way the
    contact book matches, because the feed writes "Maria Berg" and the letter may
    carry "maria berg".
    """
    if angle is None:
        return {}
    since = reference - dt.timedelta(days=ALREADY_PITCHED_DAYS)
    rows = session.execute(
        select(Outreach.journalist, Outreach.outlet, Outreach.released_at)
        .where(
            Outreach.angle_id == angle.id,
            Outreach.released_at.is_not(None),
            Outreach.released_at >= since,
        )
    ).all()
    latest: dict[tuple[str, str], dt.datetime] = {}
    for journalist, outlet, released in rows:
        key = ((journalist or "").casefold(), (outlet or "").casefold())
        if key not in latest or released > latest[key]:
            latest[key] = released
    return latest


def _enrich(
    session: Session,
    target: PitchTarget,
    pitched: dict[tuple[str, str], dt.datetime],
) -> PitchTarget:
    """What the book and the ledger already know about this byline.

    Two lookups, both of them recorded facts rather than inferences: what the
    consultant typed into the contact book — the only source of contact details in
    the tool — and whether this angle already went to this person, released and
    dated, so the list warns before proposing the same subject to them twice.
    """
    known = (
        contactbook.find(session, target.journalist, target.outlet)
        if target.journalist
        else None
    )
    if known is not None:
        target = replace(target, contact_id=known.id, contact_email=known.email)
    written = pitched.get(
        ((target.journalist or "").casefold(), target.outlet.casefold())
    )
    if written is not None:
        target = replace(target, already_pitched_at=written)
    return target


def targets_for(
    session: Session,
    client: Client,
    angle: Angle | None = None,
    *,
    now: dt.datetime | None = None,
) -> list[PitchTarget]:
    """Who to send this draft to, best signal first.

    ``angle`` is optional: without one this is the client's standing pitch list
    for its field, which is what the market view wants. With one, the people who
    wrote the stories it answers come first — they are on the subject today.
    """
    reference = now or dt.datetime.now(dt.UTC)
    since = reference - dt.timedelta(days=LOOKBACK_DAYS)
    covered = _covered_client(session, client, since)
    pitched = _already_pitched(session, angle, reference)

    targets: list[PitchTarget] = []
    seen: set[tuple[str, str | None]] = set()

    def _add(target: PitchTarget) -> None:
        key = (target.outlet.casefold(), (target.journalist or "").casefold() or None)
        if key in seen or len(targets) >= MAX_TARGETS:
            return
        seen.add(key)
        targets.append(_enrich(session, target, pitched))

    # 1. The bylines on the very stories this draft answers.
    if angle is not None:
        for author, outlet, headline in _authors_of(session, list(angle.article_ids or [])):
            _add(
                PitchTarget(
                    outlet=outlet,
                    journalist=author,
                    reason="hat die Meldung geschrieben, auf die sich dieser Impuls bezieht",
                    evidence=(headline,),
                    about_client=covered.get(outlet, 0),
                )
            )

    # 2. Everyone else writing about the field.
    for author, outlet, count, headline in _field_authors(session, client, since):
        _add(
            PitchTarget(
                outlet=outlet,
                journalist=author,
                reason=f"schreibt über das Themenfeld ({count} Meldung(en) in {LOOKBACK_DAYS} Tagen)",
                evidence=(headline,),
                about_client=covered.get(outlet, 0),
            )
        )

    # 3. Outlets on the field with no byline to name — still a target, and the
    #    ones that never mention the client are the point of the exercise.
    for outlet, count, headline in _field_outlets(session, client, since):
        about = covered.get(outlet, 0)
        _add(
            PitchTarget(
                outlet=outlet,
                journalist=None,
                reason=(
                    f"berichtet über das Themenfeld ({count} Meldung(en)), "
                    + ("bisher nie über den Mandanten" if not about else f"und {about}× über den Mandanten")
                    + ("" if count >= _MIN_FIELD_ARTICLES else " — bislang eine einzelne Meldung, kein Beat")
                ),
                evidence=(headline,),
                about_client=about,
            )
        )

    return targets


__all__ = [
    "ALREADY_PITCHED_DAYS",
    "LOOKBACK_DAYS",
    "MAX_TARGETS",
    "PitchTarget",
    "targets_for",
]
