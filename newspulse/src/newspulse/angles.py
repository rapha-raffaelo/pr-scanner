"""Positioning drafts: what to send a client when their market moves.

The rest of the tool answers "who wrote about my client?". This answers a
different question, the one a PR consultant is actually paid for: *a development
in the client's field just happened — is there something they should be saying
about it, and what?* The trigger is coverage the client is not in. That is not a
gap in the matching, it is the whole point: by the time a mandate is named in an
article, the moment to position has usually passed.

How it differs from :mod:`newspulse.advisor`
--------------------------------------------
The advisor writes *inward*: internal to-dos for the consultant, on request, from
coverage of the client. This writes *outward*: prose the consultant can forward
to the client, unasked, from coverage of the market. Two directions, two tables,
no shared row.

Design posture
--------------
**Still advisory.** Nothing is sent anywhere. The draft lands in a column, the
consultant reads it, edits it, decides. He is the accountable party and the tool
never removes him from that position — it only spares him the blank page.

**Silence is the default answer.** The prompt is explicit that most days hold no
opening, and :func:`suggest` returns ``None`` for ``worth_sending: false``. A
draft the consultant has to throw away costs him more time than an empty column,
so the bar is deliberately high and nothing is stored below it.

**The model sees headlines, not articles.** No body text is ever fetched
(Leistungsschutzrecht), so the prompt forbids stating a date, figure or name that
is not in the supplied list. A draft is a starting point with real sources
attached, never a finished quote.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from string import Template

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, guide, prose
from .analyzer import (
    AnalyzerError,
    ParseError,
    invoke_with_fallback,
    strip_code_fence,
)
from .models import Analysis, Angle, Article, Client, visible_coverage
from .schemas import AngleDraft

_log = logging.getLogger(__name__)

_PROMPT_RESOURCE = "prompts/angle.txt"

# Ceiling on the developments handed to the model. One radar feed returns a
# handful of items a day; this bounds a pathological day (a term that turns out
# to be a news staple) without truncating a normal one.
_MAX_DEVELOPMENTS = 12

# How much of the client's *own* recent coverage goes in as background, so the
# draft can avoid repeating a point the client already made publicly this week.
_OWN_COVERAGE_DAYS = 7
_MAX_OWN_COVERAGE = 8



@dataclass(frozen=True, slots=True)
class Development:
    """One numbered market development, as the prompt presented it."""

    index: int
    article_id: int
    headline: str
    source: str
    published_at: dt.datetime
    summary: str | None
    url: str


def _prompt_template() -> Template:
    text = resources.files("newspulse").joinpath(_PROMPT_RESOURCE).read_text("utf-8")
    return Template(text)


def _client_profile(client: Client) -> str:
    """What the model needs to judge whether the client may credibly speak.

    The website is included because it is often the only thing that says what the
    company actually does — an industry label like "Fashion" does not.
    """
    parts = [f"Name: {client.name}"]
    if client.industry:
        parts.append(f"Branche: {client.industry}")
    if client.website:
        parts.append(f"Website: {client.website}")
    if client.keywords:
        parts.append(f"Themen: {', '.join(client.keywords)}")
    if client.alert_topics:
        parts.append(f"Beobachtete Themen: {', '.join(client.alert_topics)}")
    return "\n".join(parts)


def developments(items: list[tuple[Article, str]]) -> list[Development]:
    """Number the radar's articles for the prompt, newest first.

    ``items`` pairs a stored article with the feed it came from. Newest first
    rather than by relevance: an opening is a moment, and the most recent
    development is the one the client would be reacting to.
    """
    ordered = sorted(items, key=lambda pair: pair[0].published_at, reverse=True)
    return [
        Development(
            index=i,
            article_id=article.id,
            headline=article.title,
            source=article.source,
            published_at=article.published_at,
            summary=article.summary_text,
            url=article.url,
        )
        for i, (article, _feed) in enumerate(ordered[:_MAX_DEVELOPMENTS])
    ]


def _render_developments(items: list[Development]) -> str:
    lines = []
    for item in items:
        stamp = item.published_at.astimezone(config.local_zone())
        line = f"[{item.index}] {stamp:%d.%m.} ({item.source}): {item.headline}"
        if item.summary:
            line += f"\n     {item.summary}"
        lines.append(line)
    return "\n".join(lines)


def _own_coverage_block(session: Session, client_id: int) -> str:
    """The client's own recent coverage, as a labelled block or nothing at all.

    Passed as a whole block (header included) so the prompt has no dangling
    section on the common case of a mandate nobody wrote about this week.
    """
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=_OWN_COVERAGE_DAYS)
    rows = session.execute(
        select(Article)
        .join(Analysis, Analysis.article_id == Article.id)
        .where(
            Analysis.client_id == client_id,
            visible_coverage(),
            Article.published_at >= since,
        )
        .order_by(Analysis.importance_score.desc(), Article.published_at.desc())
        .limit(_MAX_OWN_COVERAGE)
    ).all()
    if not rows:
        return ""
    headlines = "\n".join(f"- ({a.source}): {a.title}" for (a,) in rows)
    return (
        f"BERICHTERSTATTUNG ÜBER DEN MANDANTEN, LETZTE {_OWN_COVERAGE_DAYS} TAGE\n"
        "Nur als Hintergrund: sag nichts, was er hier schon gesagt hat.\n"
        f"{headlines}\n"
    )


def _parse(raw: str) -> AngleDraft:
    """Validate the model's text into a draft; anything else is a ParseError.

    Same trust boundary as the analyzer and the advisor: the reply is text until
    the schema says otherwise, and a draft that fails validation is discarded
    rather than half-rendered.

    A markdown code fence is unwrapped first, using the analyzer's own helper —
    wrapping JSON in ```json is a habit, not an error, and unlike the batch
    analyzer there is no retry here to absorb it: one fence would cost the day's
    draft. Anything else non-JSON stays a failure, logged rather than salvaged.
    """
    try:
        payload = json.loads(strip_code_fence(raw))
    except json.JSONDecodeError as exc:
        raise ParseError(f"angle was not valid JSON: {exc}") from exc
    try:
        return AngleDraft.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 — pydantic raises its own type
        raise ParseError(f"angle did not match the schema: {exc}") from exc


def suggest(
    session: Session,
    client: Client,
    items: list[tuple[Article, str]],
    *,
    invoke=invoke_with_fallback,
    note: Callable[[str], None] | None = None,
) -> tuple[AngleDraft, list[Development]] | None:
    """Draft a positioning message for ``client``, or ``None`` if there is none.

    ``None`` covers both "no developments to react to" and the model's own
    ``worth_sending: false`` — from the caller's point of view those are the same
    outcome (nothing to show), and neither is an error. A backend failure, by
    contrast, raises :class:`AnalyzerError`: "nothing to say" and "the draft
    failed" must never look alike.

    ``note`` receives the reason when the answer is "nothing to send". The model
    writes a genuinely useful one — "the hits are isolated competitor decisions
    with no fresh hook" — and it used to go to the log and nowhere else, so the
    reader pressed the button, watched the spinner, and got a page that looked
    exactly like a broken one. A refusal the reader can read is a result; a
    silent one is indistinguishable from a bug.

    ``invoke`` is injectable so tests drive the whole path without a subprocess.
    """
    numbered = developments(items)
    if not numbered:
        return None

    prompt = _prompt_template().substitute(
        client_profile=_client_profile(client),
        # What the mandate stands for, if anyone has written it down. Without it
        # the draft invents a voice on every call; with it, the No-Gos are a rule
        # the text has to obey rather than context it may weigh.
        comms_guide=guide.for_prompt(client),
        developments=_render_developments(numbered),
        own_coverage=_own_coverage_block(session, client.id),
    )
    draft = _parse(invoke(prompt, timeout=config.ANALYZER_TIMEOUT))
    if not draft.worth_sending:
        reason = (draft.subject or "").strip()
        _log.info(
            "no positioning opening for %r: %s", client.name, reason or "no reason given"
        )
        if note is not None:
            note(reason or "Das Modell sah keinen tragfähigen Anlass.")
        return None
    if not draft.message.strip():
        # worth_sending with no text is a contradiction; treat it as the model
        # failing to answer rather than storing an empty card.
        raise ParseError("angle claimed worth_sending but carried no message")

    # Drop evidence ids the model invented, exactly as the advisor does: a
    # citation pointing at nothing reads as sloppiness in the whole draft.
    valid = range(len(numbered))
    cleaned = draft.model_copy(
        update={"evidence": [i for i in draft.evidence if i in valid]}
    )
    return cleaned, numbered


def store(
    session: Session,
    client: Client,
    draft: AngleDraft,
    numbered: list[Development],
) -> Angle:
    """Persist a draft, resolving its evidence ids to stored article ids.

    Falls back to *all* the developments it was shown when the model cited none,
    so a draft is never displayed without the coverage behind it.
    """
    by_index = {item.index: item.article_id for item in numbered}
    cited = [by_index[i] for i in draft.evidence if i in by_index]
    # The message and the subject are what a consultant forwards, so they follow
    # the house rule on dashes (newspulse.prose). The reasoning fields below stay
    # verbatim: they are read inside the tool, never sent, and a dash there is
    # nobody's tell.
    angle = Angle(
        client_id=client.id,
        subject=prose.plain(draft.subject),
        message=prose.plain(draft.message),
        context=draft.context.strip(),
        credibility=draft.credibility.strip(),
        thesis=draft.thesis.strip(),
        overclaim=draft.overclaim.strip(),
        statements=[s.strip() for s in draft.statements if s.strip()],
        article_ids=cited or [item.article_id for item in numbered],
    )
    session.add(angle)
    session.commit()
    return angle


#: How far back the column looks for a draft. An opening does not expire at
#: midnight: the market development it rests on is still current a few days later,
#: and a column that empties itself every night hides work that is still usable.
#: A week is where it stops being current — past that the news has moved.
COLUMN_DAYS = 7


def recent(session: Session, until: dt.datetime, *, days: int = COLUMN_DAYS) -> list[Angle]:
    """The newest draft per client in the ``days`` before ``until``.

    One per client, deliberately. Two drafts for the same mandate in one week are
    two attempts at the same moment, not two things to send, and stacking them
    turns a column meant to be acted on into a backlog. Older ones stay reachable
    on the client's own page.

    ``until`` is the end of the viewed day, so looking at a past day shows what was
    current *then* rather than what is current now — the page is a day, and its
    right-hand column has to agree with the rest of it.
    """
    since = until - dt.timedelta(days=days)
    rows = session.scalars(
        select(Angle)
        .where(Angle.generated_at >= since, Angle.generated_at < until)
        .order_by(Angle.generated_at.desc(), Angle.id.desc())
    ).all()
    seen: set[int] = set()
    latest: list[Angle] = []
    for angle in rows:
        if angle.client_id in seen:
            continue
        seen.add(angle.client_id)
        latest.append(angle)
    return latest


def for_client(session: Session, client_id: int, *, limit: int = 5) -> list[Angle]:
    """This client's recent drafts, newest first.

    The Today column keeps one per mandate so it stays scannable; the client's own
    page is where the others remain reachable, which is the promise that column
    makes when it drops them.
    """
    return list(
        session.scalars(
            select(Angle)
            .where(Angle.client_id == client_id)
            .order_by(Angle.generated_at.desc(), Angle.id.desc())
            .limit(limit)
        ).all()
    )


def latest(session: Session, client_id: int) -> Angle | None:
    """The most recent draft for one client, whenever it was generated."""
    return session.scalars(
        select(Angle)
        .where(Angle.client_id == client_id)
        .order_by(Angle.generated_at.desc(), Angle.id.desc())
        .limit(1)
    ).first()


__all__ = [
    "AnalyzerError",
    "Development",
    "ParseError",
    "developments",
    "COLUMN_DAYS",
    "for_client",
    "recent",
    "latest",
    "store",
    "suggest",
]
