"""The advisor: suggested PR actions from a client's recent coverage.

The analyzer judges one article at a time. This reads the *pattern* — a month of
coverage for one client, ranked — and proposes what to do about it. That is the
work a PR manager does on Monday morning, and it is the one thing the tool could
compute that it previously left entirely to the reader.

.. note::

   **No longer on any page.** Its panel sat next to the positioning drafts and
   the difference between the two could not be stated by the person using them
   daily — "das ist wirklich nicht ganz klar wo der unterschied liegt". The
   answer was that a recommendation described work where an impulse did some, so
   the panel became a button on the impulse (:mod:`newspulse.outreach`) and the
   material this module reads — a mandate's own coverage — now personalises a
   pitch instead of forming a second column. The module and its stored briefs
   are kept: the reading it does is sound, and nothing is gained by dropping a
   table mid-flight.

Design posture
--------------
**Advisory, never autonomous.** Nothing here writes to a client, schedules
anything, or changes the tool's own behaviour. It produces a brief a human reads
and decides on. That is not a limitation to be lifted later: recommending a PR
response is a judgement with reputational consequences, and the accountable party
must be a person.

**Every suggestion cites its evidence.** The prompt requires ids from the
supplied coverage, and :func:`advise` resolves them back to the articles so the
UI can show what a suggestion is based on. A recommendation you cannot trace is
one you cannot check.

**An empty brief is a valid answer.** The prompt says so explicitly. A tool that
invents busywork on a quiet week trains its user to ignore it.

It reuses the analyzer's ``claude -p`` path, so it inherits the same
subscription-only access rule (no API token is read here) and the same strict
parse-and-validate boundary: the model's text is never trusted as structure.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from importlib import resources
from string import Template

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import brain, config, guide
from .analyzer import (
    AnalyzerError,
    BackendError,
    ParseError,
    invoke_with_fallback,
    strip_code_fence,
)
from .models import Advisory, Analysis, Article, Client, visible_coverage
from .schemas import AdvisoryBrief, without_provenance

_log = logging.getLogger(__name__)

_PROMPT_RESOURCE = "prompts/advisory.txt"

# How much coverage the brief is based on. A month is the reporting rhythm of an
# agency — but it is the wrong default for the mandates that need the brief most.
# A young or quiet company is covered a few times a quarter, so a thirty-day
# window opened on "0 Artikel in 30 Tagen" and offered nothing, while its actual
# coverage sat six weeks back in the archive. Ninety days matches the impulse's
# own lookback, so both halves of the page speak about the same period.
DEFAULT_DAYS = 90

# Ceiling on the coverage handed to the model. Ranked by importance first, so the
# cap drops the least consequential items rather than an arbitrary tail — and it
# keeps one call inside the same wall-clock budget as an analysis batch.
_MAX_COVERAGE_ITEMS = 40



@dataclass(frozen=True, slots=True)
class CoverageRef:
    """One numbered piece of coverage, as the prompt presented it."""

    index: int
    headline: str
    source: str
    published_at: dt.datetime
    category: str
    importance: int
    is_alert: bool
    url: str


def _prompt_template() -> Template:
    text = resources.files("newspulse").joinpath(_PROMPT_RESOURCE).read_text("utf-8")
    return Template(brain.compose(text))


def _client_profile(client: Client) -> str:
    parts = [f"Name: {client.name}"]
    if client.industry:
        parts.append(f"Branche: {client.industry}")
    if client.aliases:
        parts.append(f"Aliasse: {', '.join(client.aliases)}")
    if client.keywords:
        parts.append(f"Themen: {', '.join(client.keywords)}")
    if client.alert_topics:
        parts.append(f"Alarm-Themen: {', '.join(client.alert_topics)}")
    return "\n".join(parts)


def recent_coverage(
    session: Session, client_id: int, *, days: int = DEFAULT_DAYS
) -> list[CoverageRef]:
    """The client's most consequential coverage in the window, newest-relevant first.

    Ordered by importance rather than date: the brief is about what matters, and
    the cap below should drop trivia, not last week.
    """
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
    rows = session.execute(
        select(Article, Analysis)
        .join(Analysis, Analysis.article_id == Article.id)
        .where(
            Analysis.client_id == client_id,
            visible_coverage(),
            Article.published_at >= since,
        )
        .order_by(Analysis.importance_score.desc(), Article.published_at.desc())
        .limit(_MAX_COVERAGE_ITEMS)
    ).all()
    return [
        CoverageRef(
            index=i,
            headline=article.title,
            source=article.source,
            published_at=article.published_at,
            category=analysis.category.value,
            importance=analysis.importance_score,
            is_alert=analysis.is_alert,
            url=article.url,
        )
        for i, (article, analysis) in enumerate(rows)
    ]


def coverage_count(session: Session, client_id: int, *, days: int = DEFAULT_DAYS) -> int:
    """How much coverage the window actually holds, uncapped.

    :func:`recent_coverage` stops at :data:`_MAX_COVERAGE_ITEMS`, which is right
    for the prompt and wrong for the label beside the window picker: it read
    "40 Artikel" for thirty days, ninety and a hundred and eighty alike, so the
    control the reader uses to widen the window reported no change when they did.
    """
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
    return (
        session.scalar(
            select(func.count())
            .select_from(Analysis)
            .join(Article, Article.id == Analysis.article_id)
            .where(
                Analysis.client_id == client_id,
                visible_coverage(),
                Article.published_at >= since,
            )
        )
        or 0
    )


def _render_coverage(items: list[CoverageRef]) -> str:
    return "\n".join(
        f"[{item.index}] {item.published_at.astimezone(config.local_zone()):%d.%m.} "
        f"({item.source}, {item.category}, Wichtigkeit {item.importance}"
        f"{', ALARM' if item.is_alert else ''}): {item.headline}"
        for item in items
    )


def _parse(raw: str) -> AdvisoryBrief:
    """Validate the model's text into a brief; anything else is a ParseError.

    Same trust boundary as the analyzer: the reply is text until the schema says
    otherwise. A brief that fails validation is discarded rather than partially
    rendered, because a half-parsed recommendation is worse than none.

    A markdown code fence is unwrapped first (the analyzer's own helper). Like
    :mod:`newspulse.angles` and unlike the batch analyzer, this is a single call
    with no retry behind it, so a ```json wrapper would otherwise cost the whole
    brief at the moment someone asked for it.

    The stamp is taken out of the reply before validation: provenance is the
    tool's to state and not the model's. See
    :func:`newspulse.schemas.without_provenance`.
    """
    try:
        payload = json.loads(strip_code_fence(raw))
    except json.JSONDecodeError as exc:
        raise ParseError(f"advisory was not valid JSON: {exc}") from exc
    try:
        return AdvisoryBrief.model_validate(without_provenance(payload))
    except Exception as exc:  # noqa: BLE001 — pydantic raises its own type
        raise ParseError(f"advisory did not match the schema: {exc}") from exc


def advise(
    session: Session,
    client: Client,
    *,
    days: int = DEFAULT_DAYS,
    invoke=invoke_with_fallback,
) -> tuple[AdvisoryBrief, list[CoverageRef]]:
    """Generate a brief for ``client``. Raises :class:`AnalyzerError` on failure.

    ``invoke`` is injectable so tests drive the whole path without a subprocess.
    It defaults to the fallback-aware caller, so an exhausted subscription
    produces a brief from the backup provider rather than an error the operator
    has to interpret at the moment they wanted advice.
    Deliberately raises rather than returning an empty brief on a backend error:
    "nothing to advise" and "the advisor failed" must not look alike to the
    operator.

    The brief carries the brain version its prompt was composed under, captured
    here rather than read again by :func:`store`, so a standard edited while the
    model was writing belongs to the next brief and not to this one.
    """
    written_under = brain.version(session)
    coverage = recent_coverage(session, client.id, days=days)
    if not coverage:
        # Stamped too, though no prompt was composed and this sentence is one the
        # module wrote itself. What the stamp records is which standards were in
        # force when the row was made, and they were these. Leaving it NULL would
        # store the one value the whole change reserves for "written before the
        # standards were recorded" — and the page says exactly that in words, so
        # a brief made this morning would render a false claim about its own age.
        return (
            AdvisoryBrief(
                situation="Keine Berichterstattung im Zeitraum.",
                brain_version=written_under,
            ),
            [],
        )

    prompt = _prompt_template().substitute(
        client_profile=_client_profile(client),
        comms_guide=guide.for_prompt(client),
        days=days,
        coverage=_render_coverage(coverage),
    )
    raw = invoke(prompt, timeout=config.ANALYZER_TIMEOUT)
    brief = _parse(raw)

    # Drop evidence ids the model invented: an index that points at nothing would
    # render as a broken citation, which reads as sloppiness in the whole brief.
    valid = range(len(coverage))
    cleaned = [
        suggestion.model_copy(
            update={"evidence": [i for i in suggestion.evidence if i in valid]}
        )
        for suggestion in brief.suggestions
    ]
    return (
        brief.model_copy(
            update={"suggestions": cleaned, "brain_version": written_under}
        ),
        coverage,
    )


def store(
    session: Session,
    client: Client,
    brief: AdvisoryBrief,
    coverage: list[CoverageRef],
    *,
    days: int = DEFAULT_DAYS,
) -> Advisory:
    """Persist a brief as history. The newest row is the current view.

    The brain version comes off the brief: :func:`advise` captured it with the
    prompt, and reading it again here would date the row to when it was saved.
    Raises :class:`newspulse.brain.Unstamped` for a brief that carries none —
    :func:`advise` stamps both of its paths, so an unstamped one came from
    somewhere else and a NULL would file it as older than the recorded standards.
    """
    advisory = Advisory(
        client_id=client.id,
        covered_days=days,
        article_count=len(coverage),
        situation=brief.situation,
        suggestions=[s.model_dump(mode="json") for s in brief.suggestions],
        brain_version=brain.stamp(brief.brain_version, what="this brief"),
    )
    session.add(advisory)
    session.commit()
    return advisory


def latest(session: Session, client_id: int) -> Advisory | None:
    """The most recent brief for a client, or ``None`` if never generated."""
    return session.scalars(
        select(Advisory)
        .where(Advisory.client_id == client_id)
        .order_by(Advisory.generated_at.desc(), Advisory.id.desc())
        .limit(1)
    ).first()


__all__ = [
    "AnalyzerError",
    "BackendError",
    "CoverageRef",
    "DEFAULT_DAYS",
    "coverage_count",
    "ParseError",
    "advise",
    "latest",
    "recent_coverage",
    "store",
]
