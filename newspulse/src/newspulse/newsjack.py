"""The fast lane's engine: standing, origin and the window (UHR-04, DEC-6 A).

Three questions the tool did not ask before, and each has one owner here:

* **Who had it first.** The story grouping counts pickups; :func:`scan` also
  names the *origin* — the story's earliest piece, resolved by
  :func:`newspulse.stories.origin`, with timestamp ties broken by retrieval
  order rather than chance. A story whose first piece is four hours old and
  just gained its third outlet is rising; one whose first piece ran yesterday
  is through.
* **Does the mandate have standing.** Answered by one model call against
  profile, guide and archive, and the answer is one of exactly three
  (:class:`~newspulse.models.Standing`). Only ``belegt`` produces an
  opportunity; ``duenn`` and ``keins`` are stored as rejections with their
  reason, which is both the audit trail and what stops the next scan from
  paying for the same verdict again.
* **How long it still holds.** A window runs from the origin piece and closes
  by comparison against the clock — ``window_ends_at`` is fixed at creation,
  so an opportunity expires on time even if no run ever happens again. An
  opportunity without decay is a task list that only grows.

What a scan costs, and when. The clustering, the origin and every gate before
the standing check are reads over stored rows — no network, no model. A model
call happens only for a story that carries at least :data:`MEDIA_THRESHOLD`
outlets, does not mention the mandate, is still inside its window, and has no
stored verdict yet. A scan over a quiet radar therefore costs nothing, which
is what makes an every-three-hours cadence affordable (DEC-6).

Nothing here fetches a feed. The light run (:func:`newspulse.job.run_newsjack`)
refreshes the radar first and then calls :func:`scan` over what is stored.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from importlib import resources
from string import Template

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import brain, config, profile, prose, stories
from .analyzer import ParseError, invoke_with_fallback, strip_code_fence
from .matching import mentions_client, name_matcher
from .models import (
    Analysis,
    Article,
    Client,
    NewsjackOpportunity,
    Standing,
    TopicHit,
    visible_coverage,
)

_log = logging.getLogger(__name__)

_PROMPT_RESOURCE = "prompts/newsjack.txt"

#: How many distinct outlets a story needs before it is weighed at all — and
#: before a single model call is spent on it. Two, per DEC-6: one outlet is an
#: article, two is a story that is travelling.
MEDIA_THRESHOLD = 2

#: How far past the window the scan reads stored radar material, as a multiple
#: of the window. Wider than the window on purpose: the origin of a story is
#: its earliest piece *wherever* that piece falls, and a lookback equal to the
#: window would re-originate an old story at its newest pickup — turning a wave
#: that is through into one that looks fresh. Twice the window is enough to see
#: an origin that already expired, and the read is a bounded database query.
_LOOKBACK_FACTOR = 2

#: How many of the mandate's own recent pieces the standing check is shown.
#: A dozen bounds the prompt the way ``plan._MAX_CANDIDATES`` does; the archive
#: section exists to show a track record, not the whole archive.
_MAX_ARCHIVE = 12

#: How many story members the prompt lists. A wave is a handful of pickups;
#: whatever a freak cluster carries beyond this is cut newest-last, and the
#: pickup count on the row still counts every outlet.
_MAX_STORY_LINES = 10


@dataclass(frozen=True, slots=True)
class _Radar:
    """One stored radar article in the shape the clusterer needs.

    ``headline``/``source``/``importance`` are the clusterer's protocol;
    ``published_at`` is what :func:`newspulse.stories.origin` reads, and the
    article rides along so no caller looks the row up a second time. Radar
    material carries no analysis for this mandate by construction, so the
    importance is honestly zero.
    """

    headline: str
    source: str
    importance: int
    published_at: dt.datetime
    article: Article


# --- What the model is asked for -------------------------------------------------
#
# Deliberately narrow: one verdict, one sentence. ``extra="ignore"`` drops
# whatever else the model volunteers before it can reach a row.


class StandingVerdict(BaseModel):
    """The model's answer: one of three standings, and what it rests on."""

    model_config = ConfigDict(extra="ignore")

    standing: str
    reason: str = ""


#: The spellings a model plausibly answers with, folded onto the closed set.
#: "dünn" is how the word is actually written; the stored value transliterates
#: it the way ``ungeprueft`` and ``veroeffentlicht`` do.
_STANDING_SPELLINGS = {
    "belegt": Standing.BELEGT,
    "duenn": Standing.DUENN,
    "dünn": Standing.DUENN,
    "keins": Standing.KEINS,
}


def _parse(raw: str) -> tuple[Standing, str]:
    """The verdict out of the model's answer, or :class:`ParseError`.

    A fourth answer is refused rather than filed under one of the three: a
    misfiled standing either spends a consultant's morning or silences a real
    opening, and a refused parse merely costs one retry on the next scan.
    """
    try:
        payload = json.loads(strip_code_fence(raw))
    except json.JSONDecodeError as exc:
        raise ParseError(f"standing verdict was not valid JSON: {exc}") from exc
    try:
        verdict = StandingVerdict.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 — pydantic raises its own type
        raise ParseError(f"standing verdict did not match the schema: {exc}") from exc
    standing = _STANDING_SPELLINGS.get(verdict.standing.strip().casefold())
    if standing is None:
        raise ParseError(f"not a standing: {verdict.standing!r}")
    return standing, prose.plain(verdict.reason.strip())


# --- Reading the radar ------------------------------------------------------------


def _radar_rows(
    session: Session, client: Client, *, since: dt.datetime
) -> list[_Radar]:
    """The mandate's stored radar material since ``since``, in retrieval order.

    Ordered by id — the order the articles were fetched and stored — because
    that is the tie-break the origin rule promises: "bei gleicher Zeitangabe
    entscheidet die Abrufreihenfolge, nicht der Zufall". The mandate's own
    visible coverage is excluded the way :func:`newspulse.job.market_material`
    excludes it: a story the mandate is in is coverage, not an opening.
    """
    own_coverage = (
        select(Analysis.article_id)
        .where(Analysis.client_id == client.id, visible_coverage())
        .scalar_subquery()
    )
    rows = session.scalars(
        select(Article)
        .join(TopicHit, TopicHit.article_id == Article.id)
        .where(
            TopicHit.client_id == client.id,
            Article.id.not_in(own_coverage),
            Article.published_at >= since,
        )
        .order_by(Article.id.asc())
    ).all()
    return [
        _Radar(
            headline=article.title,
            source=article.source,
            importance=0,
            published_at=article.published_at,
            article=article,
        )
        for article in rows
    ]


def _already_weighed(
    session: Session, client: Client, members: tuple[_Radar, ...]
) -> bool:
    """Whether any piece of this story already carries a stored verdict.

    Checked against every member rather than only the origin, so a story whose
    origin shifts — an earlier piece surfacing late, a pickup arriving after
    the verdict — still counts as the same story. This is the read half of
    "dieselbe Story je Mandat höchstens einmal"; the UNIQUE over
    (client, article) is the write half that catches a racing second process.
    """
    ids = [member.article.id for member in members]
    return (
        session.scalars(
            select(NewsjackOpportunity.id).where(
                NewsjackOpportunity.client_id == client.id,
                NewsjackOpportunity.article_id.in_(ids),
            )
        ).first()
        is not None
    )


# --- The standing check -----------------------------------------------------------


def _prompt_template() -> Template:
    text = resources.files("newspulse").joinpath(_PROMPT_RESOURCE).read_text("utf-8")
    return Template(brain.compose(text))


def _archive_lines(session: Session, client: Client) -> str:
    """What the mandate was recently covered on — the track-record half of the
    standing question. Headlines only, newest first, honestly empty."""
    rows = session.execute(
        select(Article, Analysis)
        .join(Analysis, Analysis.article_id == Article.id)
        .where(Analysis.client_id == client.id, visible_coverage())
        .order_by(Article.published_at.desc())
        .limit(_MAX_ARCHIVE)
    ).all()
    if not rows:
        return "Keine Berichterstattung hinterlegt."
    return "\n".join(
        f"- {article.published_at:%d.%m.%Y}: {article.title} ({article.source})"
        for article, _analysis in rows
    )


def _fact_lines(session: Session, client: Client) -> str:
    """The filled profile fields, or the honest sentence that none exist."""
    facts = profile.stored(session, client.id)
    lines = [
        f"{field.label}: {facts[field.key].value}"
        for field in profile.FIELDS
        if field.key in facts and facts[field.key].value.strip()
    ]
    return "\n".join(lines) or "Noch nichts hinterlegt."


def _story_lines(members: tuple[_Radar, ...], origin: _Radar) -> str:
    """The story as the prompt shows it: the origin named, the pickups listed."""
    lines = [
        f"Ursprung ({origin.published_at:%d.%m.%Y %H:%M} UTC): "
        f"{origin.headline} ({origin.source})"
    ]
    pickups = [member for member in members if member.article.id != origin.article.id]
    lines.extend(
        f"- Aufgriff: {member.headline} ({member.source})"
        for member in pickups[: _MAX_STORY_LINES - 1]
    )
    return "\n".join(lines)


def _check_standing(
    session: Session,
    client: Client,
    members: tuple[_Radar, ...],
    origin: _Radar,
    invoke,
) -> tuple[Standing, str] | None:
    """One model call: does this mandate have standing on this story?

    ``None`` means the check could not be read — nothing is stored, so the next
    scan asks again. That is deliberately different from ``duenn``: an
    unreadable answer is not evidence about the mandate.
    """
    prompt = _prompt_template().substitute(
        client_name=client.name,
        industry=client.industry or "—",
        guide=(client.comms_guide or "").strip() or "Kein Guide hinterlegt.",
        facts=_fact_lines(session, client),
        archive=_archive_lines(session, client),
        story=_story_lines(members, origin),
    )
    try:
        return _parse(invoke(prompt, timeout=config.ANALYZER_TIMEOUT))
    except Exception as exc:  # noqa: BLE001 — one story must not cost the scan
        _log.warning(
            "standing check for %r on %r failed: %s; storing nothing, "
            "the next scan asks again",
            client.name,
            origin.headline,
            exc,
        )
        return None


# --- The scan ---------------------------------------------------------------------


def _reference(now: dt.datetime | None) -> dt.datetime:
    """``now`` as an aware moment — the same reading :func:`newspulse.plan._reference`
    gives a naive value, for the same reason."""
    reference = now or dt.datetime.now(dt.UTC)
    if reference.tzinfo is None:
        return reference.replace(tzinfo=dt.UTC)
    return reference


def window(*, hours: int | None = None) -> dt.timedelta:
    """The configured newsjack window, as a timedelta."""
    return dt.timedelta(hours=config.newsjack_window_hours() if hours is None else hours)


def scan(
    session: Session,
    client: Client,
    *,
    invoke=None,
    now: dt.datetime | None = None,
) -> list[NewsjackOpportunity]:
    """Weigh this mandate's stored radar stories; return the rows newly stored.

    The gates, in the order they cost nothing to nearly everything:

    * fewer than :data:`MEDIA_THRESHOLD` outlets — not a story yet, no call;
    * the mandate appears in any piece — that is coverage, not an opening;
    * the window from the origin piece has passed — the story is through,
      and nothing is stored for it (there is nothing to decide);
    * any piece already carries a stored verdict — decided once, never twice.

    Only a story past all four reaches the model, and whatever verdict comes
    back is stored: ``belegt`` as the opportunity, ``duenn``/``keins`` as the
    rejection with its reason. An unreadable verdict stores nothing.
    """
    reference = _reference(now)
    span = window()
    rows = _radar_rows(session, client, since=reference - _LOOKBACK_FACTOR * span)
    if not rows:
        return []
    resolved_invoke = invoke if invoke is not None else invoke_with_fallback
    matcher = name_matcher(client)
    created: list[NewsjackOpportunity] = []
    for story in stories.cluster(rows):
        if story.pickup_count < MEDIA_THRESHOLD:
            continue
        members: tuple[_Radar, ...] = story.members
        if any(mentions_client(member.article, matcher) for member in members):
            continue
        origin = stories.origin(story)
        ends = origin.published_at + span
        if reference >= ends:
            # Expired without a run having happened — the window ran from the
            # origin piece, not from anybody noticing it.
            continue
        if _already_weighed(session, client, members):
            continue
        # Captured before the model call, the way plan.recompute does it: an
        # edit landing mid-scan must not change what a stored reason claims to
        # have been written under.
        written_under = brain.version(session)
        verdict = _check_standing(session, client, members, origin, resolved_invoke)
        if verdict is None:
            continue
        standing, reason = verdict
        row = NewsjackOpportunity(
            client_id=client.id,
            article_id=origin.article.id,
            standing=standing,
            reason=reason,
            pickup_count=story.pickup_count,
            window_ends_at=ends,
            created_at=reference,
            brain_version=brain.stamp(written_under, what="a newsjack verdict"),
        )
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            # ``uq_newsjack_client_article`` fired: a concurrent scan weighed
            # the same story first. Its verdict is the verdict.
            session.rollback()
            _log.info(
                "a concurrent scan weighed %r for %r first; keeping its verdict",
                origin.headline,
                client.name,
            )
            continue
        created.append(row)
        _log.info(
            "story weighed for %r: %r is %s (%d outlet(s), window ends %s)",
            client.name,
            origin.headline,
            standing.value,
            story.pickup_count,
            ends.isoformat(),
        )
    return created


# --- Reading what stands ----------------------------------------------------------


def open_opportunities(
    session: Session, client: Client, *, now: dt.datetime | None = None
) -> list[NewsjackOpportunity]:
    """The mandate's opportunities that still hold: ``belegt``, not waved off,
    window not yet passed — soonest to expire first, because that is the order
    a consultant has to look at them in.

    Expiry is the comparison made here, against the stored end and the clock.
    No run has to happen for an opportunity to stop being returned.
    """
    reference = _reference(now)
    return list(
        session.scalars(
            select(NewsjackOpportunity)
            .where(
                NewsjackOpportunity.client_id == client.id,
                NewsjackOpportunity.standing == Standing.BELEGT,
                NewsjackOpportunity.dismissed_at.is_(None),
                NewsjackOpportunity.window_ends_at > reference,
            )
            .order_by(NewsjackOpportunity.window_ends_at.asc())
        ).all()
    )


def is_expired(
    opportunity: NewsjackOpportunity, *, now: dt.datetime | None = None
) -> bool:
    """Whether this opportunity's window has passed. A pure comparison — the
    row is never touched, so expiry holds without any run having happened."""
    return _reference(now) >= opportunity.window_ends_at


__all__ = [
    "MEDIA_THRESHOLD",
    "StandingVerdict",
    "is_expired",
    "open_opportunities",
    "scan",
    "window",
]
