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
from dataclasses import asdict, dataclass, replace
from importlib import resources
from string import Template

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import advisor, config, guide, outreach, prose, quoting, reporting
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

    Only ``kind`` is required, and that is the whole design of this schema. Every
    other field is lenient because a schema failure here is not local: pydantic
    raises for the *reply*, so one sloppy finding takes the four good ones beside
    it down with it. ``kind`` is the one fault worth paying that for — see
    :func:`_parse`. The looseness costs nothing, because none of it is trusted:
    a finding that arrives without a claim, or citing nothing, meets exactly the
    same discard rule in :func:`_accept` as one that arrived complete and empty.
    """

    #: ``coerce_numbers_to_str`` is for ``"figures": [1, 3]``, which pydantic v2
    #: otherwise refuses outright. A model that answered with the number of the
    #: reference instead of its name made a formatting slip about one finding, and
    #: ``F1`` is reconstructed from it below; it has not invented a figure, which
    #: is what the reject-whole rule in :func:`_accept` is for.
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)

    kind: ReportFindingKind
    claim: str = ""
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
    #: Which figures the claim stands on. Generation-time only — it does not
    #: outlive the draft, because :func:`store` writes the rows and not the
    #: references that produced them. What survives is the evidence itself, which
    #: is what a reader of the document opens.
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

    The trade this makes: a value that was narrowed is a *citation* view and no
    longer a recomputable one, because a share of voice re-derived from the
    mandate's rows alone comes back as 100 %, having lost its denominator. The
    narrowed value says so — :attr:`newspulse.reporting.MetricValue.citation_only`,
    which :func:`newspulse.reporting.recompute` refuses — rather than leaving the
    warning in this docstring where a caller would have to have read it. Metrics
    for that check come from :func:`newspulse.reporting.period_metrics`, which
    keeps the whole comparison set.
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
            citation_only=True,
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


def _number_text(key: MetricKey, figure: float) -> str:
    """One figure of one metric, formatted the one way this tool prints it.

    A single formatter for every consumer — the prompt, the document's tiles, the
    frozen snapshot and the charts. The figure a document states is frozen at
    release precisely so it cannot drift; a second format string somewhere else,
    kept in step by nobody, is how it would drift anyway.
    """
    if key is MetricKey.SHARE_OF_VOICE:
        return f"{figure * 100:.1f} %"
    return f"{figure:g}"


def share_text(share: float) -> str:
    """A share of voice as the document prints it, ``0.4137`` -> ``41.4 %``.

    Public because a chart also has to print a share the metric set never carried
    as a figure of its own — the *rest* of the comparison set, which is one minus
    the mandate's part of it. It goes through the same formatter as the number it
    sits beside, so the two halves of one bar cannot be printed to different
    precisions.
    """
    return _number_text(MetricKey.SHARE_OF_VOICE, share)


def _figure_text(value: MetricValue) -> str:
    """The number as the document would print it."""
    return _number_text(value.key, value.figure)


def _previous_number(value: MetricValue) -> str:
    """The comparison period's number alone, without the words around it."""
    return _number_text(value.key, value.previous)


def _previous_text(value: MetricValue) -> str:
    """The comparison, as the *prompt* states it: German, one line, with the word.

    The document builds this sentence from its parts instead (see
    :class:`Figure`), because its chrome translates and a pre-composed German
    phrase is one an English reader would be handed untranslated. The prompt has
    no such problem: it is always German and it is read by the model.
    """
    if value.previous is None:
        return "kein Vergleichszeitraum"
    return f"vorher {_previous_number(value)}, {value.direction.value}"


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
    return quoting.fence(
        "\n".join(
            f"- {article.published_at.astimezone(config.local_zone()):%d.%m.} "
            f"({article.source}, {analysis.tonality.value}"
            f"{', ALARM' if analysis.is_alert else ''}): {article.title}"
            for article, analysis in rows
        ),
        label="Schlagzeilen des Monats",
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
    mandate's own rules from :func:`newspulse.guide.for_prompt`, the figure labels
    and qualifications from the metrics themselves, and the list of what this tool
    may never state from :data:`newspulse.reporting.FORBIDDEN_FIGURES`. A prompt
    that retyped any of them would be a second copy of the house standard, drifting
    from the first the day either changed.

    The profile block is a reuse rather than a standard: ``outreach``, ``angles``
    and ``analyzer`` each render their own, with different fields, so there is no
    shared one to compose. This borrows the advisor's because it is the one that
    carries the alarm topics a risk finding needs — the same reach across a module
    boundary ``coach.py:149`` makes for ``advisor._render_coverage``.
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

    Note where the line falls, because it is not where the discard rule falls. A
    finding with a bad *kind* fails here and takes the whole reply with it, while a
    finding with a bad figure reference, or no claim, or no figures at all, is
    dropped alone by :func:`_accept`. The difference is what the fault says about
    the reply: a missing field or a wrong reference is the model being sloppy about
    one sentence, but ``kind`` is a closed four-member set spelled out in the
    prompt, and a reply that invents a fifth was not answering the question that
    was asked. The coach draws the same line for the same reason.

    Which is why :class:`ProposedFinding` requires ``kind`` and nothing else. Every
    lenient field there is this rule, expressed where pydantic can act on it:
    reject-whole is expensive — nothing above retries, so the whole report is lost
    for one call — and it must therefore be spent only on the fault that says the
    rest of the reply cannot be trusted either. A report quietly missing four good
    findings because the fifth omitted a claim is the worse outcome, and the one
    nobody would have seen.
    """
    try:
        payload = json.loads(strip_code_fence(raw))
    except json.JSONDecodeError as exc:
        raise ParseError(f"report findings were not valid JSON: {exc}") from exc
    try:
        return Proposal.model_validate(payload)
    except ValidationError as exc:
        raise ParseError(f"report findings did not match the schema: {exc}") from exc


def _normalise_ref(ref: str) -> str:
    """One figure reference as the prompt offered it.

    Two habits are read rather than rejected, because neither invents anything.
    ``[F1]`` is the exact form :func:`_render_figures` prints, and a bare ``1`` is a
    model answering with a reference's number instead of its name. Both still have
    to name a figure that is in the set; the reject-whole rule in :func:`_accept`
    is for the one that is not, and emptying a report over a formatting habit is
    not what it is for.
    """
    text = ref.strip().strip("[]").strip().upper()
    return f"{_FIGURE_PREFIX}{text}" if text.isdigit() else text


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


def _without_own_wording(text: str, cited: Sequence[MetricValue]) -> str:
    """``text`` with the key messages *this finding cites* taken out of it.

    The carve-out :class:`newspulse.reporting.MetricValue` already makes for a
    MESSAGE subject, extended to the sentence that cites it. A guide whose key
    message is "Reichweite in der Zielgruppe" is the client's own wording quoted
    back at them, so RPT-01 lets that subject through the guard and
    :func:`_render_figures` duly prints it into the prompt as a figure to write a
    ``botschaft`` finding about — and without this, the finding the prompt asked
    for is thrown away by the same guard that let the figure exist.

    Scoped to ``cited`` rather than to the whole figure set, which is the
    difference between a carve-out and a hole. One mandate whose guide happens to
    name a forbidden term in a key message would otherwise turn the guard off for
    every claim in its report, including a ``sichtbarkeit`` finding that never went
    near the message and is free to invent a reach number. A finding that does not
    cite the message figure has no claim on the client's own wording.

    Removed rather than whitelisted: what is checked afterwards is everything the
    model wrote *around* the quote, so "die Reichweite lag bei 1,2 Millionen"
    beside the message is still refused.
    """
    for value in cited:
        subject = value.subject.strip()
        if value.key is MetricKey.MESSAGE and subject:
            text = re.sub(re.escape(subject), " ", text, flags=re.IGNORECASE)
    return text


def _states_forbidden_figure(text: str, cited: Sequence[MetricValue]) -> str:
    """The forbidden figure ``text`` states, or ``""`` — the key messages this
    finding cites not counting against it."""
    terms = reporting.forbidden_terms(_without_own_wording(text, cited))
    return ", ".join(terms)


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

    refs = tuple(
        dict.fromkeys(
            normalised
            for normalised in (_normalise_ref(ref) for ref in proposed.figures)
            if normalised
        )
    )
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
    # Only the figures this finding actually stands on: the key-message carve-out
    # below is the mandate's own wording quoted back inside the claim that quotes
    # it, not a licence the report carries everywhere.
    cited = [figures[ref] for ref in refs]
    stated = _states_forbidden_figure(claim, cited) or _states_forbidden_figure(
        consequence, cited
    )
    if stated:
        # The term is named rather than implied: the matcher is deliberately blunt
        # (substring, so "geschätzte Reichweite" is caught alongside "Reichweite"),
        # which means a German sentence can trip it on a word that is not a figure
        # at all. Whoever reads the log should not have to guess which one it was.
        _log.info(
            "report finding for %r rejected: states %s, a figure this tool may not "
            "produce (%r)",
            client_name,
            stated,
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


#: What a generator has to look like: ``generate(prompt, timeout=...) -> str``, the
#: shape :func:`newspulse.analyzer.invoke_with_fallback` has and the one every test
#: stub reproduces.
Generate = Callable[..., str]


def findings(
    session: Session,
    client: Client,
    period: Period,
    *,
    generate: Generate = invoke_with_fallback,
) -> ReportDraft:
    """Propose the handful of things worth saying about ``client``'s ``period``.

    ``generate`` is injectable so the whole path runs without reaching a model; it
    defaults to the fallback-aware caller, so an exhausted subscription drafts from
    the backup provider rather than failing at the moment a report was asked for.

    Raises :class:`newspulse.analyzer.ParseError` on an unusable reply and
    :class:`newspulse.analyzer.BackendError` on a failed call, because "nothing to
    say about this month" and "the generation failed" must not look alike to the
    person about to send a document. Raises :class:`ReportReleased` for a period
    already released — :func:`store` is where that rule is enforced, but refusing
    here as well means a regeneration nobody could have filed does not first cost a
    model call, which is the most expensive thing in this path.
    """
    released = for_period(session, client.id, period)
    if released is not None and is_released(released):
        raise _released(period)

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
    if len(accepted) > MAX_FINDINGS:
        # The cap is a drop like any other, and the rule is that a drop is logged
        # rather than shown. These are findings that passed every guard, so a
        # consultant wondering why an expected sixth statement is not in the
        # document has nowhere else to look for the answer.
        _log.info(
            "report for %r keeps %d of %d accepted findings; dropped: %s",
            client.name,
            MAX_FINDINGS,
            len(accepted),
            "; ".join(finding.claim for finding in accepted[MAX_FINDINGS:]),
        )
    return ReportDraft(
        client_id=client.id,
        period=period,
        findings=tuple(accepted[:MAX_FINDINGS]),
        note="" if accepted else _NOTHING_WORTH_SAYING,
    )


# --- Storing, and what may not be overwritten ---------------------------------------


def previous_month(now: dt.datetime | None = None) -> Period:
    """The last month that has ended, in the reader's zone.

    What a report is about when nobody named a period: the scheduled draft on the
    first, and the surface's own default. A month still running would produce a
    document that is wrong the day after it was written, and the reader's zone
    rather than the host's because a piece published at 00:30 Berlin time on the
    first belongs to the month a client would put it in.
    """
    local = (now or dt.datetime.now(dt.UTC)).astimezone(config.local_zone())
    last_day = local.replace(day=1) - dt.timedelta(days=1)
    return Period.month(last_day.year, last_day.month)


def for_period(session: Session, client_id: int, period: Period) -> Report | None:
    """This mandate's report for exactly this window, if one exists."""
    return session.scalars(
        select(Report).where(
            Report.client_id == client_id,
            Report.period_start == period.start,
            Report.period_end == period.end,
        )
    ).first()


def is_released(report: Report) -> bool:
    """Whether this row is a document that went out, by either fact that says so.

    Both, and either is enough. ``released_at`` is the fact and ``state`` is the
    label the interface renders it under; :func:`release` writes the two together,
    but a restore or a route somebody writes next can set one without the other and
    both directions have to fail closed. The label alone is the worse half: a row
    reading ``freigegeben`` is what a consultant sees, so replacing its content
    leaves a released document whose sentences changed underneath it.
    """
    return report.released_at is not None or report.state is ReportState.FREIGEGEBEN


def _released(period: Period) -> ReportReleased:
    """The refusal, worded once for the two places that raise it."""
    return ReportReleased(
        f"Der Bericht für {_period_text(period)} ist freigegeben und wird nicht "
        "überschrieben."
    )


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

    Raises :class:`ValueError` if the draft was generated for another mandate. The
    draft carries the id it was measured for, and filing it under a different
    client would produce a document about one company built on another's coverage —
    exactly the failure this feature exists to make impossible.

    Raises :class:`ValueError` too for a finding with no evidence. The discard rule
    in :func:`_accept` already keeps one out of a generated draft, but this is the
    boundary the table is behind, and :class:`newspulse.models.ReportFinding` says
    of its ``evidence_ids`` that they are never empty. A hand-built draft, a future
    route, or an edit path must not be able to make that sentence false: what is
    stored without evidence is a claim standing on nothing, and no reader
    downstream can tell it apart from one whose ground merely moved.
    """
    if draft.client_id != client.id:
        raise ValueError(
            f"report draft belongs to client {draft.client_id}, not {client.id}"
        )
    groundless = [finding for finding in draft.findings if not finding.evidence_ids]
    if groundless:
        raise ValueError(
            f"report finding carries no evidence: {groundless[0].claim!r}"
        )
    existing = for_period(session, client.id, draft.period)
    if existing is not None and is_released(existing):
        raise _released(draft.period)
    row = existing or Report(
        client_id=client.id,
        period_start=draft.period.start,
        period_end=draft.period.end,
    )
    row.note = draft.note
    row.generated_at = now or dt.datetime.now(dt.UTC)
    # Asserted on every path rather than left to the column default, which only
    # applies on insert. The guard above means a row reaching here is not released,
    # so this is belt and braces — but the state and the content are one fact, and
    # writing them together is what keeps a redrafted row from ever carrying a
    # label that describes sentences it no longer holds.
    row.state = ReportState.ENTWURF
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
    """Record that a person put the agency's name on this report, and freeze it.

    Releasing an already-released report leaves the first stamp alone, exactly as
    :func:`newspulse.outreach.release` does: the record is of the moment it went
    out, and a second click is not a second sending.

    The freeze happens here rather than in the route, so there is no path that
    releases without it. A draft renders from ids resolved against the archive as
    it is, which is what lets a claim notice its ground moved; a released document
    may not, because it is what a client was sent and a piece of coverage
    dismissed next month must not change what this quarter's report said. So the
    document is built once, at this moment, and copied into
    :attr:`newspulse.models.Report.snapshot`.
    """
    if report.released_at is not None:
        return report
    client = session.get(Client, report.client_id)
    if client is None:
        raise ValueError(f"report {report.id} has no mandate to release it for")
    report.released_at = when or dt.datetime.now(dt.UTC)
    report.released_by = (
        by or outreach.DEFAULT_RELEASED_BY
    ).strip() or outreach.DEFAULT_RELEASED_BY
    report.state = ReportState.FREIGEGEBEN
    # Built *after* the stamp, so the frozen document carries the release it is
    # the record of rather than an empty one.
    report.snapshot = _payload(_live_document(session, client, report))
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
        """Whether this claim is standing on less than the whole of its ground.

        ``unsupported`` is part of the answer rather than a separate case, because
        of how a consumer uses this: a render path gating on ``weakened`` is asking
        "may I print this plainly?", and the one finding it must never print
        plainly is the one with nothing under it at all. Counting only ``missing``
        would answer *yes* for a row whose ``evidence_ids`` were empty to begin
        with — nothing failed to resolve, so nothing was lost — which is the wrong
        answer given in the most confident possible way.
        """
        return self.missing > 0 or self.unsupported

    @property
    def unsupported(self) -> bool:
        """Whether *all* of it is gone. A claim with nothing left is not weak, it
        is out — the render path drops it rather than weakening it."""
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


def resolve(
    session: Session, report: Report, *, kept_only: bool = True
) -> list[FindingView]:
    """The report's findings with their evidence as it stands today.

    Only the findings a consultant kept, unless ``kept_only`` is off. A dropped
    finding stays in the table on purpose — what was proposed and rejected is part
    of how a report was arrived at — but this is the render path, and the row
    surviving is not the sentence surviving. The review surface is the one caller
    that wants both, because that is the screen on which the dropping happens and
    a finding that vanished on a click cannot be argued about afterwards.

    One query for the whole report rather than one per finding: a report carries a
    handful of findings and a document renders all of them at once.

    A finding whose evidence has gone entirely comes back too, marked ``weakened``
    and ``unsupported``. Dropping it here would be this module deciding what a
    document contains; the render path is where "not weak, it is out" is applied,
    and a caller reviewing a report is entitled to see that one of its claims lost
    its ground rather than to find a sentence quietly absent.
    """
    kept = [
        finding for finding in report.findings if finding.kept or not kept_only
    ]
    # Deduped per finding. ``evidence_ids`` is a citation list, and one piece of
    # coverage cited twice is one piece of evidence: undeduped it prints the same
    # headline twice under a single claim and makes ``missing`` count one dismissed
    # article as two losses. :func:`_evidence_for` dedups at generation, but a row
    # written by hand or by a later edit route never passes through it.
    cited = [tuple(dict.fromkeys(finding.evidence_ids)) for finding in kept]
    rows = _evidence_rows(
        session, report.client_id, [row for ids in cited for row in ids]
    )
    return [
        FindingView(
            finding=finding,
            evidence=tuple(rows[row] for row in ids if row in rows),
            missing=sum(1 for row in ids if row not in rows),
        )
        for finding, ids in zip(kept, cited, strict=True)
    ]


# --- The document ------------------------------------------------------------------
#
# What a report *is* once it leaves the review surface: a dated page with the
# period's figures on it, the findings a consultant kept, and the coverage under
# each of them. Built here rather than in the route because it is also what gets
# frozen — the same object serves the screen, the export and the snapshot, so a
# released document cannot differ from the one that was approved.


@dataclass(frozen=True, slots=True)
class Figure:
    """One printed number, already rendered to the words the document uses.

    Rendered here rather than in the template because it is also what is frozen:
    a share stored as ``0.4137`` and formatted at render time would come out of
    next year's template with a different number of decimals, which is a released
    document changing after the fact for a reason nobody would ever look for.
    """

    #: :class:`newspulse.reporting.MetricKey` as a string. A string rather than the
    #: enum so the frozen form round-trips through JSON unchanged.
    key: str
    label: str
    #: Which tonality this is, where the figure has one.
    subject: str
    #: The number as the document prints it, ``""`` when there is none.
    text: str
    #: The bare number, for the geometry of a chart. ``None`` where there is none.
    value: float | None
    #: The comparison period's number, formatted the same way and frozen for the
    #: same reason. ``""`` where there is no comparison to make. Stored as the
    #: number alone rather than as "vorher 12, gestiegen": the words around it are
    #: chrome, and chrome translates, while the number may not move.
    previous_value_text: str
    #: ``gestiegen``/``gefallen``/``unverändert``, or ``unbekannt`` where there is
    #: nothing to compare against. ``""`` where there is no figure at all — which
    #: is what tells the two apart on the page.
    direction: str
    #: Why there is no number, or the qualification the reader needs with it.
    note: str


@dataclass(frozen=True, slots=True)
class DocumentFinding:
    """One kept finding as the document prints it, with its evidence attached."""

    kind: str
    claim: str
    consequence: str
    #: Whether a person rewrote the sentence. A reviewer of the document is
    #: entitled to know which claims are as generated.
    edited: bool
    #: Whether some of the ground under it had gone by the time this was built.
    weakened: bool
    evidence: tuple[EvidenceRow, ...]


@dataclass(frozen=True, slots=True)
class Document:
    """Everything the rendered report says, in one shape.

    One object for three consumers — the page, the export and the snapshot — so
    "the export carries the same content as the on-screen document" is true by
    construction rather than by two templates being kept in step.
    """

    client_name: str
    period_start: dt.datetime
    period_end: dt.datetime
    generated_at: dt.datetime
    released_at: dt.datetime | None
    released_by: str
    #: The mandates the share of voice is measured against, named. A comparison set
    #: that is not on the page is a denominator the reader cannot check.
    comparison: tuple[str, ...]
    figures: tuple[Figure, ...]
    tonality: tuple[Figure, ...]
    findings: tuple[DocumentFinding, ...]
    note: str

    @property
    def tonality_total(self) -> float:
        """The tonality split's denominator, for the chart's geometry."""
        return sum(figure.value or 0.0 for figure in self.tonality)

    @property
    def period_last(self) -> dt.datetime:
        """The last day the period contains.

        ``period_end`` is exclusive, and a document headed "1.7. bis 1.8." reads as
        covering a day it does not.
        """
        return self.period_end - dt.timedelta(days=1)


def _stated_text(value: MetricValue) -> str:
    """The number as the document prints it, or ``""`` where there is none."""
    return "" if value.figure is None else _figure_text(value)


def _figure(value: MetricValue) -> Figure:
    return Figure(
        key=value.key.value,
        label=value.label,
        subject=value.subject,
        text=_stated_text(value),
        value=value.figure,
        previous_value_text=(
            "" if value.figure is None or value.previous is None
            else _previous_number(value)
        ),
        # Empty where there is no figure, so a tile with nothing on it says
        # nothing about a comparison either. With a figure and no baseline the
        # direction is ``unbekannt``, which is what the page renders as "no
        # comparison period" rather than as a movement of zero.
        direction="" if value.figure is None else value.direction.value,
        note=value.note,
    )


def _document_finding(view: FindingView) -> DocumentFinding:
    return DocumentFinding(
        kind=view.finding.kind.value,
        claim=view.finding.claim,
        consequence=view.finding.consequence,
        edited=view.finding.edited_at is not None,
        weakened=view.weakened,
        evidence=view.evidence,
    )


def _live_document(session: Session, client: Client, report: Report) -> Document:
    """The document as the archive reads right now.

    A claim whose evidence has gone entirely is absent here rather than weakened.
    :func:`resolve` deliberately hands it over — the review surface must show that
    a claim lost its ground — but this is the artefact, and a sentence with nothing
    under it is not a thin finding, it is one this document may not make.
    """
    period = Period(report.period_start, report.period_end)
    metrics = reporting.period_metrics(session, client, period)
    stated = (
        metrics.coverage,
        metrics.lead_media,
        reporting.attributed_coverage(session, client, period),
        metrics.share_of_voice,
    )
    return Document(
        client_name=client.name,
        period_start=report.period_start,
        period_end=report.period_end,
        generated_at=report.generated_at,
        released_at=report.released_at,
        released_by=report.released_by,
        comparison=tuple(rival.name for rival in client.competitors),
        figures=tuple(_figure(value) for value in stated),
        tonality=tuple(_figure(value) for value in metrics.tonality),
        findings=tuple(
            _document_finding(view)
            for view in resolve(session, report)
            if not view.unsupported
        ),
        note=report.note,
    )


def _json_default(value: object) -> str:
    """Only what the document actually carries that JSON does not."""
    if isinstance(value, dt.datetime):
        return value.isoformat()
    raise TypeError(f"a document may not carry {type(value).__name__}")


def _payload(document: Document) -> dict:
    """The document as the stored snapshot holds it."""
    return json.loads(json.dumps(asdict(document), default=_json_default))


def _moment(text: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(text) if text else None


def _thawed_figure(row: dict) -> Figure:
    return Figure(
        key=row.get("key", ""),
        label=row.get("label", ""),
        subject=row.get("subject", ""),
        text=row.get("text", ""),
        value=row.get("value"),
        previous_value_text=row.get("previous_value_text", ""),
        direction=row.get("direction", ""),
        note=row.get("note", ""),
    )


def _thawed_finding(row: dict) -> DocumentFinding:
    return DocumentFinding(
        kind=row.get("kind", ""),
        claim=row.get("claim", ""),
        consequence=row.get("consequence", ""),
        edited=bool(row.get("edited")),
        weakened=bool(row.get("weakened")),
        evidence=tuple(
            EvidenceRow(
                analysis_id=cited.get("analysis_id", 0),
                headline=cited.get("headline", ""),
                source=cited.get("source", ""),
                url=cited.get("url", ""),
                published_at=_moment(cited.get("published_at")),
            )
            for cited in row.get("evidence", ())
        ),
    )


def _thaw(payload: dict) -> Document:
    """A frozen document, read back.

    Every field is read with a default rather than unpacked. A snapshot is written
    once and read for years, and the failure this avoids is the worst one this
    feature has: a released document that stops rendering because a field was
    added to :class:`Document` after it was frozen.
    """
    return Document(
        client_name=payload.get("client_name", ""),
        period_start=_moment(payload.get("period_start")),
        period_end=_moment(payload.get("period_end")),
        generated_at=_moment(payload.get("generated_at")),
        released_at=_moment(payload.get("released_at")),
        released_by=payload.get("released_by", ""),
        comparison=tuple(payload.get("comparison", ())),
        figures=tuple(_thawed_figure(row) for row in payload.get("figures", ())),
        tonality=tuple(_thawed_figure(row) for row in payload.get("tonality", ())),
        findings=tuple(_thawed_finding(row) for row in payload.get("findings", ())),
        note=payload.get("note", ""),
    )


def document(session: Session, client: Client, report: Report) -> Document:
    """What this report says: off the snapshot if it has one, off the archive if not.

    The whole immutability rule, in one branch. A draft is a reading of the archive
    and re-reads it every time, so a dismissed article weakens the claim that cited
    it. A released report has a snapshot, and from then on the archive is not
    consulted at all — which is what makes the document a client was sent still say
    next quarter what it said when it was sent.

    The middle case is a released row with no snapshot, and it is not hypothetical:
    :func:`release` returns early on an already-released report, and the column was
    added after releasing existed, so any report released before this migration
    would otherwise read off the live archive forever — unfrozen, silently, and for
    exactly the reports that most need not to be. It is frozen here instead, on
    first read, against the archive as it stands. That is later than release and
    therefore imperfect; it is the only moment still available, and a document that
    stops moving today is worth more than one that never does.
    """
    if report.snapshot:
        return _thaw(report.snapshot)
    built = _live_document(session, client, report)
    if is_released(report):
        report.snapshot = _payload(built)
        session.commit()
    return built


__all__ = [
    "Document",
    "DocumentFinding",
    "EvidenceRow",
    "Figure",
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
    "document",
    "findings",
    "for_period",
    "is_released",
    "previous_month",
    "release",
    "resolve",
    "share_text",
    "store",
]
