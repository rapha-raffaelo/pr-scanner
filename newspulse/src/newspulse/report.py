"""The report: findings, each carrying its evidence.

:mod:`newspulse.reporting` measures the month. This decides what is worth saying
about it, which is the part a client actually pays for and the part that was
retyped every month from a dashboard into a document.

The inversion
-------------
The obvious build is to hand a model the coverage and ask it for an assessment
with its sources. That produces a paragraph nobody can check: the citation is a
sentence the model wrote, and a model that has forgotten which article a number
came from will write a plausible one anyway.

So it is the other way round here. The model is given the figures and the
headlines and is asked one narrow thing: which *figures* a claim stands on. The
evidence is then attached by :func:`findings` from the ids those figures already
carry, because :mod:`newspulse.reporting` hands every metric back together with
the rows it was computed from. The model chooses which number the sentence is
about; the code decides what is underneath it.

Three consequences, and all three are the point:

* A finding whose evidence list comes out empty is discarded before a human sees
  it. A claim resting on nothing is not a weak finding, it is not a finding.
* A finding naming a figure this period did not produce is rejected whole rather
  than filtered down, because the sentence was written about a number that does
  not exist and the rest of it cannot be trusted either.
* A finding can go weak in public. The stored evidence is ids, not copied
  headlines, so an article that is later dismissed stops resolving and
  :func:`resolve` renders the claim with what is left instead of with the ground
  it used to stand on.

Silence
-------
A month with no coverage produces no findings and says why, following the
precedent :func:`newspulse.reporting.period_metrics` and
:func:`newspulse.angles.suggest` already set: an empty answer with a reason on it
is a result, and manufacturing three findings out of a quiet July would be the
first thing in this document a client could catch out.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from importlib import resources
from string import Template

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import advisor, config, guide, outreach, prose, reporting
from .analyzer import ParseError, invoke_with_fallback, strip_code_fence
from .models import (
    Analysis,
    Article,
    Client,
    Report,
    ReportFinding,
    ReportFindingKind,
    ReportState,
    visible_coverage,
)
from .reporting import MetricKey, MetricValue, Period, PeriodMetrics

_log = logging.getLogger(__name__)

_PROMPT_RESOURCE = "prompts/report_findings.txt"

#: How many findings one report may carry. The same ceiling the coach uses, for
#: the same reason: past a handful it stops being a briefing and becomes an audit,
#: and a consultant reviewing twelve of them before a jour fixe reviews none.
MAX_FINDINGS = 5

#: How much of the month's coverage goes into the prompt as context. Ranked by
#: importance first, so the cap drops trivia rather than an arbitrary tail, and one
#: call stays inside the same wall-clock budget as an analysis batch.
_MAX_HEADLINES = 30

#: The prefix every citable figure is offered under. Short and opaque on purpose:
#: a model asked for "die Tonalität" would paraphrase, and a reference either is in
#: the set or is not.
_FIGURE_PREFIX = "F"

#: Said when the period holds coverage but no figure survived to be cited. Not
#: reachable from :func:`period_metrics` today (a non-empty period always states a
#: coverage count) and kept as an honest answer rather than an assertion, because
#: the alternative is a prompt listing no figures and a model inventing some.
_NO_FIGURES = (
    "Für diesen Zeitraum liegt keine belegbare Kennzahl vor, auf die sich eine "
    "Aussage stützen ließe."
)

#: Said when the month held coverage and the reading of it produced nothing that
#: stands up. Distinct from the empty month on purpose: "nothing was written about
#: you" and "what was written carries no statement" are different sentences to put
#: in front of a client, and a report that said the first about the second would be
#: wrong about the month.
_NOTHING_WORTH_SAYING = (
    "Aus der Berichterstattung dieses Zeitraums ergibt sich keine belegbare Aussage."
)


class ReportReleased(RuntimeError):
    """A released report may not be regenerated or replaced.

    The same rule the outreach ledger applies to a released letter, and for the
    same reason: this is the artefact a client was sent, and a document that says
    something different next quarter than it said when it went out is worse than
    no document.
    """


# --- What the model is asked for -------------------------------------------------
#
# The reply schema lives here rather than in :mod:`newspulse.schemas`, which holds
# the analysis layer's shapes. What matters is that it sits beside the discard rule
# that reads it: the two are one decision about how much a returned finding is
# trusted, and splitting them across modules is how the rule ends up applied in one
# place and forgotten in another.


class ProposedFinding(BaseModel):
    """One finding as the model returned it, before anything is believed.

    Note what is *not* here: evidence ids. The model names figures; the code
    attaches the rows. A field for ids would be a field for invented ids.
    """

    model_config = ConfigDict(extra="ignore")

    kind: ReportFindingKind
    claim: str
    consequence: str = ""
    #: References into the figure set the prompt offered, e.g. ``["F1", "F3"]``.
    figures: list[str] = Field(default_factory=list)


class Proposal(BaseModel):
    """Everything the model proposed for one period."""

    model_config = ConfigDict(extra="ignore")

    findings: list[ProposedFinding] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Finding:
    """One accepted finding, with the rows the code put underneath it."""

    kind: ReportFindingKind
    claim: str
    consequence: str
    #: Analysis ids. Never empty: that is what acceptance means here.
    evidence_ids: tuple[int, ...]
    #: Which figures the claim stands on, kept for the log and for a reviewer
    #: asking why these rows and not others.
    figures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReportDraft:
    """What a period is worth saying, before anybody has read it.

    ``findings`` empty and ``note`` filled is a complete answer, not a failure.
    """

    client_id: int
    period: Period
    findings: tuple[Finding, ...] = ()
    note: str = ""


# --- The figures a finding may stand on --------------------------------------------


def _own_evidence(
    session: Session, client_id: int, values: Sequence[MetricValue]
) -> list[MetricValue]:
    """The same figures, with every id that is not this mandate's coverage removed.

    A metric's ``analysis_ids`` are the rows it was *computed* from, and for share
    of voice that is deliberately the whole comparison set — the denominator is
    part of the figure, so a reader recomputing it needs the rivals' rows too. A
    report cites something narrower: the rows a client may be shown as the ground
    under a claim about themselves. Printing a competitor's headline there would be
    wrong twice over, in the document and in :attr:`FindingView.weakened`, where a
    rival's article being dismissed would report that this mandate's claim had lost
    its footing.

    Done here, once, for every figure rather than only for share of voice: the next
    metric whose ids span mandates would otherwise reintroduce the same bug
    silently, and narrowing early also means the prompt's "N belegende Zeilen" is
    the count of what could actually be cited.
    """
    cited = {row for value in values for row in value.analysis_ids}
    if not cited:
        return list(values)
    mine = set(
        session.scalars(
            select(Analysis.id).where(
                Analysis.id.in_(list(cited)), Analysis.client_id == client_id
            )
        )
    )
    return [
        value
        if set(value.analysis_ids) <= mine
        else replace(
            value,
            analysis_ids=tuple(row for row in value.analysis_ids if row in mine),
        )
        for value in values
    ]


def citable_figures(
    session: Session,
    client: Client,
    period: Period,
    metrics: PeriodMetrics | None = None,
) -> dict[str, MetricValue]:
    """Every figure this period actually produced, under the reference it is cited by.

    The union of all three metric calls, not just :attr:`PeriodMetrics.values`:
    coverage "aus eigener Ansprache" is the strongest sentence a report can carry
    and message pull-through is the one a guide is judged by, and a whitelist built
    from the period-level figures alone would reject both.

    A figure that states no number is absent rather than present-at-zero. There is
    nothing to cite in "we hold no July", and a reference to it would let a claim
    be built on the absence of data.

    What comes back is citable in both senses: the number exists, and the ids under
    it are this mandate's own coverage — see :func:`_own_evidence`.

    ``metrics`` is passed in by :func:`findings`, which has already measured the
    period; on its own this recomputes, so a caller wanting only the citable set
    does not have to know the order of the calls.
    """
    if metrics is None:
        metrics = reporting.period_metrics(session, client, period)
    stated = [
        value
        for value in (
            *metrics.values,
            reporting.attributed_coverage(session, client, period),
            *reporting.message_pull_through(session, client, period),
        )
        if value.figure is not None
    ]
    return {
        f"{_FIGURE_PREFIX}{number}": value
        for number, value in enumerate(
            _own_evidence(session, client.id, stated), start=1
        )
    }


def _figure_text(value: MetricValue) -> str:
    """The number as the document would print it."""
    if value.key is MetricKey.SHARE_OF_VOICE:
        return f"{value.figure * 100:.1f} %"
    return f"{value.figure:g}"


def _previous_text(value: MetricValue) -> str:
    if value.previous is None:
        return "kein Vergleichszeitraum"
    if value.key is MetricKey.SHARE_OF_VOICE:
        return f"vorher {value.previous * 100:.1f} %, {value.direction.value}"
    return f"vorher {value.previous:g}, {value.direction.value}"


def _render_figures(figures: dict[str, MetricValue]) -> str:
    """The figure set as the prompt shows it, one line each.

    Every line carries how many rows are behind the number, so the model can see
    which figures have evidence to attach and which are a zero with nothing under
    it. A claim built on the second is discarded later; showing the count here is
    what keeps that from being the normal outcome.
    """
    lines = []
    for ref, value in figures.items():
        name = f"{value.label} ({value.subject})" if value.subject else value.label
        parts = [
            f"[{ref}] {name}: {_figure_text(value)}",
            _previous_text(value),
            f"{len(value.analysis_ids)} belegende Zeilen",
        ]
        if value.note:
            parts.append(value.note)
        lines.append("; ".join(parts))
    return "\n".join(lines)


def _headlines(session: Session, client_id: int, period: Period) -> str:
    """The month's coverage as context, deliberately without citable numbers."""
    rows = session.execute(
        select(Article, Analysis)
        .join(Analysis, Analysis.article_id == Article.id)
        .where(
            Analysis.client_id == client_id,
            visible_coverage(),
            Article.published_at >= period.start,
            Article.published_at < period.end,
        )
        .order_by(Analysis.importance_score.desc(), Article.published_at.desc())
        .limit(_MAX_HEADLINES)
    ).all()
    return "\n".join(
        f"- {article.published_at.astimezone(config.local_zone()):%d.%m.} "
        f"({article.source}, {analysis.tonality.value}"
        f"{', ALARM' if analysis.is_alert else ''}): {article.title}"
        for article, analysis in rows
    )


def _period_text(period: Period) -> str:
    zone = config.local_zone()
    return (
        f"{period.start.astimezone(zone):%d.%m.%Y} bis "
        # Exclusive end, printed as the last day it contains: a report headed
        # "1.7. bis 1.8." reads as covering a day it does not.
        f"{(period.end - dt.timedelta(days=1)).astimezone(zone):%d.%m.%Y}"
    )


def _prompt_template() -> Template:
    text = resources.files("newspulse").joinpath(_PROMPT_RESOURCE).read_text("utf-8")
    return Template(text)


def build_prompt(
    session: Session, client: Client, period: Period, figures: dict[str, MetricValue]
) -> str:
    """The prompt, composed rather than restated.

    Every standard in it comes from where that standard already lives: the
    mandate's own rules from :func:`newspulse.guide.for_prompt`, the profile block
    from the advisor that all the other prompts share, the figure labels and
    qualifications from the metrics themselves, and the list of what this tool may
    never state from :data:`newspulse.reporting.FORBIDDEN_FIGURES`. A prompt that
    retyped any of them would be a second copy of the house standard, drifting from
    the first the day either changed.
    """
    return _prompt_template().substitute(
        client_profile=advisor._client_profile(client),
        comms_guide=guide.for_prompt(client),
        period=_period_text(period),
        figures=_render_figures(figures),
        coverage=_headlines(session, client.id, period),
        forbidden=", ".join(sorted(reporting.FORBIDDEN_FIGURES)),
        max_findings=MAX_FINDINGS,
    )


# --- Generation, and the discard rule ---------------------------------------------


def _parse(raw: str) -> Proposal:
    """Validate the reply into a proposal; anything else is a ParseError.

    The same trust boundary as everywhere else in this codebase: the reply is text
    until the schema says otherwise, and a half-parsed report is discarded rather
    than shown.
    """
    try:
        payload = json.loads(strip_code_fence(raw))
    except json.JSONDecodeError as exc:
        raise ParseError(f"report findings were not valid JSON: {exc}") from exc
    try:
        return Proposal.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 — pydantic raises its own type
        raise ParseError(f"report findings did not match the schema: {exc}") from exc


def _evidence_for(
    refs: Iterable[str], figures: dict[str, MetricValue]
) -> tuple[int, ...]:
    """The rows behind the cited figures, deduped, in a stable order.

    A piece of coverage that carries two of the cited figures is one piece of
    evidence, the same way :func:`newspulse.reporting.attributed_coverage` counts a
    piece answering two letters once.
    """
    return tuple(
        sorted({row for ref in refs for row in figures[ref].analysis_ids})
    )


def _accept(
    proposed: ProposedFinding, figures: dict[str, MetricValue], client_name: str
) -> Finding | None:
    """One returned finding, or ``None`` and a line in the log saying why not.

    Every discard is logged rather than shown. A consultant who is told "the model
    proposed something and it was thrown away" learns nothing he can act on, and a
    rejected claim rendered with a warning is a claim that has been rendered.
    """
    # House style, enforced rather than requested, exactly as the outreach letter
    # does it: the prompt asks for no dashes and the model relapses. This text goes
    # to a client under the agency's name. See newspulse.prose.
    claim = prose.plain(proposed.claim).strip()
    consequence = prose.plain(proposed.consequence).strip()
    if not claim:
        _log.info("report finding for %r discarded: no claim", client_name)
        return None

    refs = tuple(dict.fromkeys(ref.strip().upper() for ref in proposed.figures if ref))
    unknown = [ref for ref in refs if ref not in figures]
    if unknown:
        # Rejected whole, not filtered down: the sentence was written about a
        # number that does not exist in this period, so the rest of it is not
        # trustworthy either.
        _log.info(
            "report finding for %r rejected: cites %s, which this period did not "
            "produce (%r)",
            client_name,
            ", ".join(unknown),
            claim,
        )
        return None
    if reporting.is_forbidden(claim) or reporting.is_forbidden(consequence):
        _log.info(
            "report finding for %r rejected: states a figure this tool may not "
            "produce (%r)",
            client_name,
            claim,
        )
        return None

    evidence = _evidence_for(refs, figures)
    if not evidence:
        _log.info(
            "report finding for %r discarded: no evidence behind %s (%r)",
            client_name,
            ", ".join(refs) or "any figure",
            claim,
        )
        return None
    return Finding(
        kind=proposed.kind,
        claim=claim,
        consequence=consequence,
        evidence_ids=evidence,
        figures=refs,
    )


def findings(
    session: Session,
    client: Client,
    period: Period,
    *,
    generate=invoke_with_fallback,
) -> ReportDraft:
    """Propose the handful of things worth saying about ``client``'s ``period``.

    ``generate`` is injectable so the whole path runs without reaching a model; it
    defaults to the fallback-aware caller, so an exhausted subscription drafts from
    the backup provider rather than failing at the moment a report was asked for.

    Raises :class:`newspulse.analyzer.ParseError` on an unusable reply and
    :class:`newspulse.analyzer.BackendError` on a failed call, because "nothing to
    say about this month" and "the generation failed" must not look alike to the
    person about to send a document.
    """
    metrics = reporting.period_metrics(session, client, period)
    if metrics.empty:
        # No model call at all. There is nothing to interpret, and asking anyway
        # invites three findings about a month that held nothing.
        _log.info("no report findings for %r: %s", client.name, metrics.note)
        return ReportDraft(client_id=client.id, period=period, note=metrics.note)

    figures = citable_figures(session, client, period, metrics)
    if not figures:
        _log.info("no report findings for %r: no citable figure", client.name)
        return ReportDraft(client_id=client.id, period=period, note=_NO_FIGURES)

    prompt = build_prompt(session, client, period, figures)
    proposal = _parse(generate(prompt, timeout=config.ANALYZER_TIMEOUT))
    accepted = [
        finding
        for finding in (
            _accept(proposed, figures, client.name) for proposed in proposal.findings
        )
        if finding is not None
    ]
    return ReportDraft(
        client_id=client.id,
        period=period,
        findings=tuple(accepted[:MAX_FINDINGS]),
        note="" if accepted else _NOTHING_WORTH_SAYING,
    )


# --- Storing, and what may not be overwritten ---------------------------------------


def for_period(session: Session, client_id: int, period: Period) -> Report | None:
    """This mandate's report for exactly this window, if one exists."""
    return session.scalars(
        select(Report).where(
            Report.client_id == client_id,
            Report.period_start == period.start,
            Report.period_end == period.end,
        )
    ).first()


def store(
    session: Session,
    client: Client,
    draft: ReportDraft,
    *,
    now: dt.datetime | None = None,
) -> Report:
    """Persist a draft. Generating the same period twice replaces the draft.

    One row per (client, period): a second generation is the same July read again,
    not a second July, and two rows would put two documents with the same date and
    different sentences in front of the same client.

    A *released* report is never replaced, and the attempt raises rather than
    quietly doing nothing. It is the same rule the outreach ledger applies to a
    released letter: this is the artefact the client was sent, and a document that
    changes after the fact is worse than no document.
    """
    existing = for_period(session, client.id, draft.period)
    if existing is not None and existing.state is ReportState.FREIGEGEBEN:
        raise ReportReleased(
            f"Der Bericht für {_period_text(draft.period)} ist freigegeben und "
            "wird nicht überschrieben."
        )
    row = existing or Report(
        client_id=client.id,
        period_start=draft.period.start,
        period_end=draft.period.end,
    )
    row.note = draft.note
    row.generated_at = now or dt.datetime.now(dt.UTC)
    # delete-orphan takes the previous findings with it, so a regenerated draft
    # cannot leave a claim from the last reading standing beside the new ones.
    row.findings.clear()
    row.findings.extend(
        ReportFinding(
            kind=finding.kind,
            claim=finding.claim,
            consequence=finding.consequence,
            evidence_ids=list(finding.evidence_ids),
        )
        for finding in draft.findings
    )
    session.add(row)
    session.commit()
    return row


def release(
    session: Session,
    report: Report,
    *,
    by: str = outreach.DEFAULT_RELEASED_BY,
    when: dt.datetime | None = None,
) -> Report:
    """Record that a person put the agency's name on this report.

    Releasing an already-released report leaves the first stamp alone, exactly as
    :func:`newspulse.outreach.release` does: the record is of the moment it went
    out, and a second click is not a second sending.
    """
    if report.released_at is None:
        report.released_at = when or dt.datetime.now(dt.UTC)
        report.released_by = (
            by or outreach.DEFAULT_RELEASED_BY
        ).strip() or outreach.DEFAULT_RELEASED_BY
        report.state = ReportState.FREIGEGEBEN
        session.commit()
    return report


# --- Reading a stored report back ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvidenceRow:
    """One cited piece of coverage, as it stands now."""

    analysis_id: int
    headline: str
    source: str
    url: str
    published_at: dt.datetime


@dataclass(frozen=True, slots=True)
class FindingView:
    """A stored finding with its evidence resolved against the archive as it is.

    ``missing`` is what makes the claim honest over time. An article that is
    deleted or dismissed after the report was drafted stops resolving, and the
    finding is then rendered as *weakened*, with the evidence that is left, rather
    than as a sentence whose ground moved without anybody being told.
    """

    finding: ReportFinding
    evidence: tuple[EvidenceRow, ...]
    missing: int

    @property
    def weakened(self) -> bool:
        """Whether some of what this claim stood on is gone."""
        return self.missing > 0

    @property
    def unsupported(self) -> bool:
        """Whether *all* of it is. A claim with nothing left is not weak, it is out."""
        return not self.evidence


def _evidence_rows(
    session: Session, client_id: int, analysis_ids: Sequence[int]
) -> dict[int, EvidenceRow]:
    """The cited rows that still exist, are still visible coverage, and are this
    mandate's own.

    The mandate filter is a second lock on the same door :func:`_own_evidence`
    holds shut at generation time. A row stored before that narrowing existed, or
    written by hand, must still not be able to print another company's headline as
    the ground under this client's claim — the one failure of this feature that
    would be visible to the client rather than to us.
    """
    if not analysis_ids:
        return {}
    found = session.execute(
        select(Analysis, Article)
        .join(Article, Article.id == Analysis.article_id)
        .where(
            Analysis.id.in_(list(analysis_ids)),
            Analysis.client_id == client_id,
            visible_coverage(),
        )
    ).all()
    return {
        analysis.id: EvidenceRow(
            analysis_id=analysis.id,
            headline=article.title or "",
            source=article.source or "",
            url=article.url or "",
            published_at=article.published_at,
        )
        for analysis, article in found
    }


def resolve(session: Session, report: Report) -> list[FindingView]:
    """The report's findings with their evidence as it stands today.

    Only the findings a consultant kept. A dropped finding stays in the table on
    purpose — what was proposed and rejected is part of how a report was arrived
    at — but this is the render path, and the row surviving is not the sentence
    surviving.

    One query for the whole report rather than one per finding: a report carries a
    handful of findings and a document renders all of them at once.
    """
    kept = [finding for finding in report.findings if finding.kept]
    rows = _evidence_rows(
        session,
        report.client_id,
        [row for finding in kept for row in finding.evidence_ids],
    )
    return [
        FindingView(
            finding=finding,
            evidence=tuple(
                rows[row] for row in finding.evidence_ids if row in rows
            ),
            missing=sum(1 for row in finding.evidence_ids if row not in rows),
        )
        for finding in kept
    ]


__all__ = [
    "EvidenceRow",
    "Finding",
    "FindingView",
    "MAX_FINDINGS",
    "ParseError",
    "Proposal",
    "ProposedFinding",
    "ReportDraft",
    "ReportReleased",
    "build_prompt",
    "citable_figures",
    "findings",
    "for_period",
    "release",
    "resolve",
    "store",
]
