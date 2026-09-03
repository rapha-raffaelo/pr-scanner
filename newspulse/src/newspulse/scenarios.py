"""Szenarien, Auslöser und Reaktionsoptionen (RIS-04).

What could happen next, how one would notice that it is happening, and what
one does then. Three modules' worth of discipline sits in this one, because
all three are the same discipline said about three objects.

**A scenario is never a forecast.** Three courses per issue, and each of them
has to say of itself that it is a possibility: :func:`_reads_as_scenario`
refuses a narrative written as fact, because a course that reads like a
statement is quoted back as one four weeks later. The likelihood is a *word*
from :class:`~newspulse.models.ScenarioLikelihood` and can never be a
percentage: a percentage out of a model claims an accuracy that does not
exist, and it is exactly that number which ends up in the meeting.

**A trigger is what separates a scenario from an essay.** DEC-5 locked "nur
maschinell prüfbare Bedingungen", so a trigger is one member of the closed
:class:`~newspulse.models.TriggerCondition` set and nothing else, and a
scenario whose triggers all fall outside it is not stored at all.
:func:`check_triggers` is the half that makes the set worth having: it reads
stored rows, fires what holds, and writes ``fired_at`` — a column, not a set in
memory, because "ein bereits ausgelöster Auslöser feuert nicht erneut, auch
nicht nach einem Neustart" is the acceptance and a process-local latch would
re-announce every standing trigger on the next boot.

**Nothing is numbered or dated that the stored lines do not carry.**
:func:`_unsupported_figures` walks the generated prose for digits the material
did not contain and drops the row that invented one. This is the same rule
``no_invention`` asks the model for, enforced rather than requested — the
posture ``prose.plain`` already takes for dashes.

**"Nicht reagieren" is an option like any other.** A set of response options is
stored only with at least :data:`~newspulse.models.RESPONSE_OPTIONS_MIN` of
them and only if one of them is the silence, graded on the same three columns.
A tool that can only propose acting proposes acting.

Both model calls are injectable, and no test here exercises them against a real
backend.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from importlib import resources
from string import Template

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import brain, config, outlets, profile, prose, stakeholders
from .analyzer import ParseError, invoke_with_fallback, strip_code_fence
from .matching import name_matcher, terms_matcher
from .models import (
    RESPONSE_LABEL_MAX,
    RESPONSE_OPTIONS_MIN,
    Client,
    EscalationPotential,
    Issue,
    OutreachReply,
    Outreach,
    ResponseOption,
    ResponseSpeed,
    Scenario,
    ScenarioKind,
    ScenarioLikelihood,
    ScenarioStakeholder,
    ScenarioTrigger,
    Stakeholder,
    TriggerCondition,
)

_log = logging.getLogger(__name__)

_SCENARIO_PROMPT = "prompts/scenarios.txt"
_OPTIONS_PROMPT = "prompts/response_options.txt"

#: How many of an issue's signals the two prompts list, newest first. The
#: prompt exists to show what the matter is, not the whole register row — the
#: same ten :mod:`newspulse.stakeholders` shows for the same reason.
_MAX_SIGNAL_LINES = 10

#: The outlet tier that counts as the top reach class, the same 1 the register
#: and the reading read the Leitmedien list with.
_TOP_TIER = 1

#: How many independent outlets make the ``zweites_medium`` condition hold.
#: Two, spelled out because the condition is named after it.
_SECOND_OUTLET = 2

#: The words a narrative must contain to read as a scenario rather than as a
#: statement of fact. A closed set, like every other closed set in this
#: feature: "im Konjunktiv" is not something a regular expression can decide,
#: but "does this text hedge at all" is, and a course carrying none of these
#: is a course written as a finding. Lower-cased and matched on word
#: boundaries, so "wennschon" is not a hedge and "Wenn" is.
_SCENARIO_MARKERS = frozenset(
    {
        "szenario",
        "könnte",
        "könnten",
        "kann",
        "dürfte",
        "dürften",
        "würde",
        "würden",
        "wäre",
        "wären",
        "möglich",
        "möglicherweise",
        "denkbar",
        "falls",
        "wenn",
        "sollte",
        "sollten",
        "droht",
        "drohen",
        "vermutlich",
        "womöglich",
    }
)

#: Every run of digits, which is what the figure rule compares. Deliberately
#: crude: a date, a sum, a count and a percentage are all digits to a reader
#: who is about to quote one, and "the material did not contain this number" is
#: the whole question. Words spelled out ("drei Wochen") are not caught, and
#: that is the accepted edge — a spelled-out number is not what gets quoted as
#: a measurement.
_DIGITS = re.compile(r"\d+")

#: Word characters for the marker scan, umlauts included: ``\w`` under
#: ``re.UNICODE`` already covers them, and the scan lower-cases first so
#: "Könnte" and "könnte" are one word.
_WORDS = re.compile(r"\w+", re.UNICODE)


# --- The model's answers, as pydantic reads them ----------------------------------


class ScenarioProposal(BaseModel):
    """One course as the answer names it."""

    model_config = ConfigDict(extra="ignore")

    art: str
    verlauf: str = ""
    wahrscheinlichkeit: str = ""
    ausloeser: list[str] = []
    stakeholder: list[str] = []
    kommunikationsbedarf: str = ""


class ScenarioSet(BaseModel):
    """The model's three courses."""

    model_config = ConfigDict(extra="ignore")

    szenarien: list[ScenarioProposal] = []


class OptionProposal(BaseModel):
    """One response option as the answer names it."""

    model_config = ConfigDict(extra="ignore")

    option: str
    nutzen: str = ""
    risiko: str = ""
    eskalationspotenzial: str = EscalationPotential.MITTEL.value
    nicht_reagieren: bool = False
    empfohlen: bool = False
    geschwindigkeit: str = ""


class OptionSet(BaseModel):
    """The model's options, in the order it offers them."""

    model_config = ConfigDict(extra="ignore")

    optionen: list[OptionProposal] = []


# --- Small shared helpers ----------------------------------------------------------


def _parse(raw: str, schema: type[BaseModel]) -> BaseModel:
    """The payload out of the model's answer, or :class:`ParseError`."""
    try:
        payload = json.loads(strip_code_fence(raw))
    except json.JSONDecodeError as exc:
        raise ParseError(f"scenario answer was not valid JSON: {exc}") from exc
    try:
        return schema.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 — pydantic raises its own type
        raise ParseError(f"scenario answer did not match the schema: {exc}") from exc


def _template(resource: str) -> Template:
    text = resources.files("newspulse").joinpath(resource).read_text("utf-8")
    return Template(brain.compose(text))


def _signal_lines(issue: Issue) -> str:
    """The issue's signals as the prompts show them — stored lines and nothing
    else. This is also the material the figure rule measures against."""
    rows = sorted(issue.signals, key=lambda row: row.happened_at, reverse=True)
    lines = []
    for row in rows[:_MAX_SIGNAL_LINES]:
        if row.article is not None:
            lines.append(
                f"- {row.happened_at:%d.%m.%Y}: {row.article.title} "
                f"({row.article.source})"
            )
        elif row.market_signal is not None:
            lines.append(
                f"- {row.happened_at:%d.%m.%Y}: Marktsignal: "
                f"{row.market_signal.title}"
            )
    return "\n".join(lines) or "Keine Signale gespeichert."


def _issue_line(issue: Issue) -> str:
    """The matter in one line: the title, and the description where there is one."""
    title = issue.title
    if (issue.description or "").strip():
        title += f", {issue.description.strip()}"
    return title


def _reads_as_scenario(text: str) -> bool:
    """Whether this narrative says of itself that it is a possibility.

    The mechanical half of "jedes Szenario ist im Text als Szenario
    gekennzeichnet". A course carrying none of :data:`_SCENARIO_MARKERS` is
    written as a finding — "Die Behörde eröffnet ein Verfahren und der Vorstand
    tritt zurück" — and a finding about the future is the one thing this
    feature must not produce, because it is quoted back as one.
    """
    words = {word.lower() for word in _WORDS.findall(text or "")}
    return bool(words & _SCENARIO_MARKERS)


def _unsupported_figures(text: str, material: str) -> list[str]:
    """Digit runs in ``text`` that ``material`` does not carry, in order.

    "Das Modell liefert keine Zahl und kein Datum, die nicht in einer benannten
    Zeile stehen", enforced instead of asked for. Compared as strings rather
    than as values: a reader quoting "1,2 Millionen" back at a client quotes
    the characters, and a normalisation that made 1.200.000 and 1,2 the same
    figure would let one of them through under the other's authority.
    """
    supported = set(_DIGITS.findall(material))
    return [figure for figure in _DIGITS.findall(text or "") if figure not in supported]


def _clean(text: str) -> str:
    """One line of generated prose as it is stored: dash-flattened and trimmed."""
    return prose.plain((text or "").strip())


# --- The three scenarios -----------------------------------------------------------


def stored_scenarios(session: Session, issue: Issue) -> list[Scenario]:
    """The issue's courses, in the order the three kinds are read."""
    rows = session.scalars(
        select(Scenario).where(Scenario.issue_id == issue.id)
    ).all()
    order = {kind: rank for rank, kind in enumerate(ScenarioKind)}
    return sorted(rows, key=lambda row: order[row.kind])


def _scenario_row(
    proposal: ScenarioProposal,
    *,
    issue: Issue,
    material: str,
    reference: dt.datetime,
    written_under: int | None,
) -> Scenario | None:
    """One proposed course as it is filed, or ``None`` where it is dropped.

    Five rules decide, and every one of them is a drop rather than a repair:
    an unknown kind, an unknown likelihood word (a percentage lands here), a
    narrative that reads as fact, a figure the material does not carry, and —
    in :func:`_triggers_for`, which the caller applies — no checkable trigger.
    """
    try:
        kind = ScenarioKind(proposal.art.strip().lower())
    except ValueError:
        _log.warning(
            "a scenario for issue %d named an unknown course %r; it is dropped "
            "rather than filed under a guess",
            issue.id,
            proposal.art,
        )
        return None
    try:
        likelihood = ScenarioLikelihood(proposal.wahrscheinlichkeit.strip().lower())
    except ValueError:
        _log.warning(
            "the %s scenario for issue %d gave the likelihood as %r, which is "
            "not one of the words; a percentage is never stored",
            kind.value,
            issue.id,
            proposal.wahrscheinlichkeit,
        )
        return None
    narrative = _clean(proposal.verlauf)
    if not narrative or not _reads_as_scenario(narrative):
        _log.warning(
            "the %s scenario for issue %d is written as a statement of fact; "
            "a course that reads as a finding is quoted back as one",
            kind.value,
            issue.id,
        )
        return None
    need = _clean(proposal.kommunikationsbedarf)
    invented = _unsupported_figures(narrative, material) + _unsupported_figures(
        need, material
    )
    if invented:
        _log.warning(
            "the %s scenario for issue %d carries figure(s) %s that stand in no "
            "named line; it is dropped rather than stored with an invented number",
            kind.value,
            issue.id,
            invented,
        )
        return None
    return Scenario(
        issue_id=issue.id,
        kind=kind,
        narrative=narrative,
        likelihood=likelihood,
        communication_need=need,
        created_at=reference,
        brain_version=brain.stamp(written_under, what="a scenario"),
    )


def _triggers_for(proposal: ScenarioProposal, *, kind: str) -> list[ScenarioTrigger]:
    """The proposal's triggers, keeping only the closed set's members.

    DEC-5 in one function: a condition the run cannot check is not a trigger,
    however well it is phrased, so anything outside
    :class:`~newspulse.models.TriggerCondition` is dropped here and the caller
    drops the whole scenario when nothing survives.
    """
    kept: list[ScenarioTrigger] = []
    seen: set[TriggerCondition] = set()
    for raw in proposal.ausloeser:
        try:
            condition = TriggerCondition(str(raw).strip().lower())
        except ValueError:
            _log.info(
                "the %s scenario named %r as a trigger, which is not a checkable "
                "condition; a trigger nothing can fire is not one",
                kind,
                raw,
            )
            continue
        if condition in seen:
            continue
        seen.add(condition)
        kept.append(ScenarioTrigger(condition=condition))
    return kept


def _groups_for(
    proposal: ScenarioProposal, by_name: dict[str, Stakeholder]
) -> list[ScenarioStakeholder]:
    """The affected groups, taken from the standing map and nowhere else."""
    rows: list[ScenarioStakeholder] = []
    seen: set[int] = set()
    for raw in proposal.stakeholder:
        target = by_name.get(_norm(str(raw)))
        if target is None or target.id in seen:
            continue
        seen.add(target.id)
        rows.append(ScenarioStakeholder(stakeholder_id=target.id))
    return rows


def _norm(name: str) -> str:
    """One group name as compared, the same rule the map itself uses."""
    return " ".join((name or "").split()).casefold()


def generate_scenarios(
    session: Session,
    issue: Issue,
    *,
    invoke=None,
    now: dt.datetime | None = None,
) -> list[Scenario]:
    """Build the issue's three courses. Idempotent: a set that stands is kept.

    Re-asking would replace narratives a consultant has already read into a
    meeting, and would re-arm triggers that have already fired — which is the
    one thing "einmal gemeldet" forbids. An issue whose set is to be rebuilt
    goes through :func:`clear_scenarios` first, deliberately, by a person.

    What is dropped, and why, is :func:`_scenario_row`'s and
    :func:`_triggers_for`'s. A scenario without a checkable trigger is not
    stored: DEC-5's whole point is that a trigger nobody can fire is not one.
    """
    standing = stored_scenarios(session, issue)
    if standing:
        return standing
    client = session.get(Client, issue.client_id)
    card = stakeholders.card(session, client)
    signals = _signal_lines(issue)
    issue_line = _issue_line(issue)
    # The material the figure rule measures against: exactly the stored lines
    # the prompt was shown, so "eine benannte Zeile" means the same thing to
    # the check as it does to the model.
    material = f"{issue_line}\n{signals}\n" + "\n".join(
        f"{row.group_name}: {row.betroffenheit}" for row in card
    )
    # Captured when the prompt is composed, not when the rows are saved: an
    # edit landing while the model writes changes the next set, not this one.
    written_under = brain.version(session)
    prompt = _template(_SCENARIO_PROMPT).substitute(
        client_name=client.name,
        issue_title=issue_line,
        signals=signals,
        map="\n".join(f"- {row.group_name}" for row in card) or "Noch keine Karte.",
    )
    resolved_invoke = invoke if invoke is not None else invoke_with_fallback
    answer = _parse(
        resolved_invoke(prompt, timeout=config.ANALYZER_TIMEOUT), ScenarioSet
    )
    by_name = {_norm(row.group_name): row for row in card}
    reference = now or dt.datetime.now(dt.UTC)
    stored: list[Scenario] = []
    seen: set[ScenarioKind] = set()
    for proposal in answer.szenarien:
        row = _scenario_row(
            proposal,
            issue=issue,
            material=material,
            reference=reference,
            written_under=written_under,
        )
        if row is None or row.kind in seen:
            continue
        triggers = _triggers_for(proposal, kind=row.kind.value)
        if not triggers:
            _log.warning(
                "the %s scenario for issue %d carries no checkable trigger and "
                "is not stored: a trigger nothing can fire is not one",
                row.kind.value,
                issue.id,
            )
            continue
        row.triggers = triggers
        row.groups = _groups_for(proposal, by_name)
        session.add(row)
        seen.add(row.kind)
        stored.append(row)
    if not stored:
        # Nothing of ours to write, so nothing is committed: an unconditional
        # commit would flush whatever the caller's session happens to hold.
        _log.info("no scenario for issue %d survived; nothing is stored", issue.id)
        return []
    try:
        session.commit()
    except IntegrityError:
        # Two clicks landed at once and the other one stored this set first.
        # Its rows are the answer, and the caller re-reads what stands.
        session.rollback()
        _log.warning(
            "a concurrent generation stored issue %d's scenarios first; the "
            "rows that stand are kept",
            issue.id,
        )
        return stored_scenarios(session, issue)
    order = {kind: rank for rank, kind in enumerate(ScenarioKind)}
    return sorted(stored, key=lambda row: order[row.kind])


def clear_scenarios(session: Session, issue: Issue) -> int:
    """Take the issue's courses off, so a person can ask again. Returns how many.

    The deliberate way past :func:`generate_scenarios`'s idempotence, and it is
    a person's decision rather than a side effect: the triggers go with the
    scenarios, which re-arms them, and re-arming a condition that has already
    been reported is the one thing the latch exists to prevent happening by
    accident.
    """
    rows = stored_scenarios(session, issue)
    for row in rows:
        session.delete(row)
    if rows:
        session.commit()
    return len(rows)


# --- The triggers the run checks ---------------------------------------------------
#
# Every reader answers the same way: the empty string where its condition does
# not hold, and *what matched* where it does — the outlet, the headline, the
# journalist, the name, and nothing else. No reader writes a sentence: which
# condition held is the stored ``condition``, and :data:`CONDITION_LABELS`
# renders it in the reader's language beside the note. A sentence composed into
# the note would be German in the database and German on the English page.


def _outlet_sources(issue: Issue) -> list[str]:
    """The outlets behind the issue's article signals, in signal order."""
    return [
        row.article.source
        for row in issue.signals
        if row.article is not None and row.article.source
    ]


def _second_outlet(session: Session, client: Client, issue: Issue) -> str:
    """A second independent outlet on the matter, or the empty string.

    ``distinct_outlets`` is the register's own idea of independence — the same
    normalisation the crisis level and the reading count with — so a wire copy
    republished under three mastheads of one house is not two media here
    either.
    """
    distinct = outlets.distinct_outlets(_outlet_sources(issue))
    if len(distinct) < _SECOND_OUTLET:
        return ""
    return ", ".join(outlets.display_name(source) for source in distinct)


def _top_tier(session: Session, client: Client, issue: Issue) -> str:
    """An outlet of the top reach class on the matter, or the empty string."""
    for source in _outlet_sources(issue):
        if outlets.tier_for(source) == _TOP_TIER:
            return outlets.display_name(source)
    return ""


def _named_in_headline(session: Session, client: Client, issue: Issue) -> str:
    """The mandate named in one of the issue's headlines, or the empty string.

    The matcher is the ingest's own (:func:`newspulse.matching.name_matcher`),
    so "named" means here exactly what it means where the coverage was matched
    in the first place — a mandate is not named in one place and unnamed in
    another.
    """
    matcher = name_matcher(client)
    if matcher is None:
        return ""
    for row in issue.signals:
        if row.article is None:
            continue
        # Case-folded, because the matcher is: its alternation is built out of
        # folded variants, so a headline handed to it as written matches
        # nothing and the condition would silently never fire.
        if matcher.search((row.article.title or "").casefold()):
            return row.article.title
    return ""


def _media_enquiry(session: Session, client: Client, issue: Issue) -> str:
    """A journalist's message in the connected mailbox, or the empty string.

    Bounded to what arrived since the issue was opened: a reply from before the
    matter began answers a different letter, and firing on it would report an
    event that predates the scenario it is supposed to start.
    """
    row = session.scalars(
        select(OutreachReply)
        .join(Outreach, Outreach.id == OutreachReply.outreach_id)
        .where(
            Outreach.client_id == client.id,
            OutreachReply.received_at >= issue.opened_at,
        )
        .order_by(OutreachReply.received_at.desc())
        .limit(1)
    ).first()
    if row is None:
        return ""
    # Who wrote, as the header carried them; the letter's own journalist or
    # outlet where it carried no name. Mailsync files a reply only when its
    # From address matches a stored contact, so one of the three stands.
    return row.sender or row.letter.journalist or row.letter.outlet


def _management_named(session: Session, client: Client, issue: Issue) -> str:
    """A person from the profile's management line in the coverage, or "".

    The names come from the stored profile and from nowhere else: a person this
    tool inferred to be an executive would put a stranger's name on a red mark
    at an issue, which is the failure ``no_invention`` exists for.

    Matched with the ingest's own matcher rather than with ``in``, and for the
    same reason :func:`_named_in_headline` uses it: the lookarounds are what
    stop "Berger" being found inside "Bergerhoff". A substring hit here is
    worse than elsewhere, because ``fired_at`` is a latch — a wrong firing
    reports the wrong thing *and* spends the trigger the real event was armed
    for.
    """
    people = _management_names(session, client)
    if not people:
        return ""
    # One matcher per name, so the mark can say which person was found; the
    # list is a line of a profile, not a corpus.
    matchers = [(person, terms_matcher([person])) for person in people]
    for row in issue.signals:
        if row.article is None:
            continue
        # Case-folded like the matcher's own alternation, which shares the
        # ß→ss fold with the ingest.
        haystack = (
            f"{row.article.title or ''} {row.article.summary_text or ''}".casefold()
        )
        for person, matcher in matchers:
            if matcher is not None and matcher.search(haystack):
                return person
    return ""


#: The profile lines a management name may come out of. Two, and both are
#: lines a person filled: the leadership line and the spokesperson line.
_MANAGEMENT_FIELDS = ("ceo", "sprecher")

#: The shortest run of characters treated as a person's name. Two-letter
#: fragments ("AG", "GF") match half the archive, and a red mark on an issue
#: is not worth a substring hit.
_MIN_NAME_CHARS = 4

#: The fewest words a part must have to be read as a person. Two, because the
#: leadership line is written as a first name and a surname, while a part of a
#: single word is a role — "Vorstand", "Geschäftsführung", "Pressestelle" —
#: and a role word stands in a large share of German business coverage. Firing
#: on one would report a role rather than a person and spend a latch that only
#: fires once, ever. A single-word name is the accepted cost: the condition
#: does not fire, rather than firing on the wrong thing.
_MIN_NAME_WORDS = 2

#: What separates one name from the next on a management line: the punctuation
#: a person actually types, plus "und". The brackets and the colon are in here
#: because that is how a *role* arrives — "Anna Berger (CEO)",
#: "Geschäftsführung: Anna Berger" — and a role left inside the part makes the
#: part a string no headline can contain.
_NAME_SEPARATORS = re.compile(r"[,;/()\n:]|\bund\b")


def _management_names(session: Session, client: Client) -> list[str]:
    """The names the profile's management lines carry, one per part.

    The ``ceo`` field is "Namen **und Rollen** der Geschäftsführung" and
    ``sprecher`` is free prose, so both arrive as a person and their function
    in one line. Splitting on :data:`_NAME_SEPARATORS` puts the two on either
    side of the cut, and the parts that survive :data:`_MIN_NAME_CHARS` and
    :data:`_MIN_NAME_WORDS` are the ones that can be a person at all. A part is
    matched whole, so "Anna Berger" is found and "Anna Berger CEO" never is.
    """
    facts = profile.stored(session, client.id)
    names: list[str] = []
    for key in _MANAGEMENT_FIELDS:
        row = facts.get(key)
        if row is None:
            continue
        for part in _NAME_SEPARATORS.split(row.value or ""):
            cleaned = " ".join(part.split())
            if len(cleaned) < _MIN_NAME_CHARS:
                continue
            if len(cleaned.split()) < _MIN_NAME_WORDS:
                continue
            names.append(cleaned)
    return names


#: What each course is called where a person reads it. Here rather than in the
#: template because every one of them is a key in the i18n table, and a label
#: assembled in Jinja cannot be looked up — it would render German on an
#: English page.
KIND_LABELS: dict[ScenarioKind, str] = {
    ScenarioKind.BESTER: "Bester Verlauf",
    ScenarioKind.WAHRSCHEINLICHER: "Wahrscheinlicher Verlauf",
    ScenarioKind.SCHLECHTESTER: "Schlechtester Verlauf",
}

#: What each condition of the closed set is called on the page. The stored
#: value is a key ("zweites_medium"); this is the sentence a reader can act on,
#: and it is a lookup key for the same reason :data:`KIND_LABELS` is.
CONDITION_LABELS: dict[TriggerCondition, str] = {
    TriggerCondition.ZWEITES_MEDIUM: "ein zweites unabhängiges Medium",
    TriggerCondition.LEITMEDIUM: "ein Medium der obersten Reichweitenklasse",
    TriggerCondition.MANDAT_IN_UEBERSCHRIFT: "das Mandat in einer Überschrift",
    TriggerCondition.MEDIENANFRAGE: "eine Medienanfrage im verbundenen Postfach",
    TriggerCondition.MANAGEMENT_GENANNT: "eine namentlich genannte Person des Managements",
}

#: Every condition of the closed set and the reader that answers it. A dict
#: rather than a chain of ``if``s so the two can never drift: a member added to
#: :class:`~newspulse.models.TriggerCondition` without a reader here fails
#: :func:`check_triggers`'s own completeness test rather than silently never
#: firing, which is exactly the "looks like a trigger, is not one" state DEC-5
#: was decided against.
CONDITION_READERS = {
    TriggerCondition.ZWEITES_MEDIUM: _second_outlet,
    TriggerCondition.LEITMEDIUM: _top_tier,
    TriggerCondition.MANDAT_IN_UEBERSCHRIFT: _named_in_headline,
    TriggerCondition.MEDIENANFRAGE: _media_enquiry,
    TriggerCondition.MANAGEMENT_GENANNT: _management_named,
}


def _by_condition(courses: list[Scenario]) -> dict[TriggerCondition, list[ScenarioTrigger]]:
    """The courses' triggers grouped by the condition each one watches for.

    The grouping is the whole of "einmal": the same condition legitimately
    stands on more than one course — a second independent outlet starts the
    likely course *and* the worst one — but it is one condition holding once,
    not two events. Read per row, an issue would report the same firing twice
    and carry two identical marks.
    """
    grouped: dict[TriggerCondition, list[ScenarioTrigger]] = {}
    for scenario in courses:
        for trigger in scenario.triggers:
            grouped.setdefault(trigger.condition, []).append(trigger)
    return grouped


def check_triggers(
    session: Session,
    client: Client,
    issue: Issue,
    *,
    now: dt.datetime | None = None,
) -> list[ScenarioTrigger]:
    """Fire the issue's standing conditions that now hold. One row back per condition.

    Reads stored rows only — no model call and no fetch — which is what lets it
    run for every open issue on every sweep. A condition already fired on this
    issue is skipped before its reader is even called: ``fired_at`` is a
    column, so the skip survives a restart, and "einmal gemeldet, und danach
    nicht wieder" holds across deployments rather than across an uptime. The
    skip is per *issue* and not per row, so a condition that fired on one
    course does not fire again through a second course carrying it.

    Every row watching a condition that holds is latched, with the same moment
    and the same note — they are all spent by the one event — and exactly one
    of them comes back, because the acceptance marks and announces the issue
    once.

    The caller notifies what comes back exactly once
    (:func:`newspulse.notify.notify_triggers`). The latch is written here
    rather than after delivery on purpose: a channel that retries until it
    succeeds re-announces every morning that SMTP is down, which is the
    always-red band this whole feature was specified against.
    """
    reference = now or dt.datetime.now(dt.UTC)
    fired: list[ScenarioTrigger] = []
    for condition, rows in _by_condition(stored_scenarios(session, issue)).items():
        if any(row.has_fired for row in rows):
            continue
        note = CONDITION_READERS[condition](session, client, issue)
        if not note:
            continue
        for row in rows:
            row.fired_at = reference
            row.fired_note = note
        # The first row, in the order the courses are read, so the mark names
        # the earliest course this condition would start. Every row is latched;
        # only one is reported.
        fired.append(rows[0])
    if fired:
        session.commit()
        _log.info(
            "%d scenario condition(s) fired on issue %d", len(fired), issue.id
        )
    return fired


def fired_marks(courses: list[Scenario]) -> list[ScenarioTrigger]:
    """The marks a set of courses carries: one per fired condition, newest first.

    One per condition rather than one per row, for the same reason
    :func:`check_triggers` fires per condition: the acceptance puts the mark on
    the *issue*, and two identical pills on a card say one event happened
    twice. Takes the courses rather than a session so the page can read it off
    the rows it has already loaded.
    """
    marks: dict[TriggerCondition, ScenarioTrigger] = {}
    for scenario in courses:
        for trigger in scenario.triggers:
            if trigger.has_fired:
                marks.setdefault(trigger.condition, trigger)
    return sorted(marks.values(), key=lambda row: row.fired_at, reverse=True)


def fired_triggers(session: Session, issue: Issue) -> list[ScenarioTrigger]:
    """The issue's fired conditions, newest firing first.

    What the register renders as the Vermerk am Issue: the condition, what
    matched, and when — a red mark that cannot say what it saw is one nobody
    can act on.
    """
    return fired_marks(stored_scenarios(session, issue))


# --- The response options ----------------------------------------------------------


def stored_options(session: Session, issue: Issue) -> list[ResponseOption]:
    """The issue's response options, in the order they are read."""
    return list(
        session.scalars(
            select(ResponseOption)
            .where(ResponseOption.issue_id == issue.id)
            .order_by(ResponseOption.position)
        ).all()
    )


def _option_row(
    proposal: OptionProposal,
    *,
    issue: Issue,
    material: str,
    position: int,
    reference: dt.datetime,
    written_under: int | None,
) -> ResponseOption | None:
    """One proposed option as it is filed, or ``None`` where it is dropped.

    The same three drops the scenarios take: a nameless option, an escalation
    word outside the closed set, and a figure the material does not carry. The
    speed is read only off the recommended row, and an unreadable one drops
    the recommendation rather than the option — the acceptance requires the
    Empfehlung to *name* a speed, so a row that cannot is not one.
    """
    label = _clean(proposal.option)[:RESPONSE_LABEL_MAX]
    if not label:
        return None
    try:
        escalation = EscalationPotential(
            proposal.eskalationspotenzial.strip().lower()
        )
    except ValueError:
        _log.warning(
            "the option %r for issue %d graded its escalation as %r, which is "
            "not one of the words; it is dropped rather than filed under a guess",
            label,
            issue.id,
            proposal.eskalationspotenzial,
        )
        return None
    benefit = _clean(proposal.nutzen)
    risk = _clean(proposal.risiko)
    invented = _unsupported_figures(
        f"{label} {benefit} {risk}", material
    )
    if invented:
        _log.warning(
            "the option %r for issue %d carries figure(s) %s that stand in no "
            "named line; it is dropped rather than stored with an invented number",
            label,
            issue.id,
            invented,
        )
        return None
    speed: ResponseSpeed | None = None
    recommended = bool(proposal.empfohlen)
    if recommended:
        try:
            speed = ResponseSpeed(proposal.geschwindigkeit.strip().lower())
        except ValueError:
            _log.warning(
                "the recommended option %r for issue %d named the speed %r, "
                "which is not one of the six; it is stored as an option and "
                "not as the recommendation",
                label,
                issue.id,
                proposal.geschwindigkeit,
            )
            recommended = False
    return ResponseOption(
        issue_id=issue.id,
        label=label,
        benefit=benefit,
        risk=risk,
        escalation=escalation,
        no_response=bool(proposal.nicht_reagieren),
        recommended=recommended,
        speed=speed,
        position=position,
        created_at=reference,
        brain_version=brain.stamp(written_under, what="a response option"),
    )


def _one_recommendation(rows: list[ResponseOption]) -> None:
    """Leave at most one row recommended: the first, in the offered order.

    A model that marks two is not offering a choice, it is offering none, and a
    page showing two Empfehlungen is a page a reader cannot act on. The later
    ones lose the mark and their speed with it, which is what the CHECK on the
    table requires anyway.
    """
    seen = False
    for row in rows:
        if not row.recommended:
            continue
        if seen:
            row.recommended = False
            row.speed = None
            continue
        seen = True


def generate_options(
    session: Session,
    issue: Issue,
    *,
    invoke=None,
    now: dt.datetime | None = None,
) -> list[ResponseOption]:
    """Build the issue's response options. Idempotent: a set that stands is kept.

    Two rules decide whether *anything* is stored, and both are the acceptance
    rather than taste:

    * at least :data:`~newspulse.models.RESPONSE_OPTIONS_MIN` options survive
      :func:`_option_row`;
    * one of them is "nicht reagieren". A set without the silence is a set that
      can only propose acting, and storing it would put exactly that list in
      front of the reader in the hour it matters most.

    Neither holding means nothing is stored and the log says which — a second
    press asks again, which is the right answer for an answer that came back
    unusable.
    """
    standing = stored_options(session, issue)
    if standing:
        return standing
    client = session.get(Client, issue.client_id)
    signals = _signal_lines(issue)
    issue_line = _issue_line(issue)
    courses = stored_scenarios(session, issue)
    scenario_lines = "\n".join(
        f"- {row.kind.value} ({row.likelihood.value}): {row.narrative}"
        for row in courses
    )
    # The same material rule the scenarios take, plus the courses themselves:
    # an option may cite a figure a stored scenario already carries, since that
    # figure passed this very check when the scenario was stored.
    material = f"{issue_line}\n{signals}\n{scenario_lines}"
    written_under = brain.version(session)
    prompt = _template(_OPTIONS_PROMPT).substitute(
        client_name=client.name,
        issue_title=issue_line,
        signals=signals,
        scenarios=scenario_lines or "Noch keine Szenarien gespeichert.",
    )
    resolved_invoke = invoke if invoke is not None else invoke_with_fallback
    answer = _parse(
        resolved_invoke(prompt, timeout=config.ANALYZER_TIMEOUT), OptionSet
    )
    reference = now or dt.datetime.now(dt.UTC)
    rows: list[ResponseOption] = []
    for proposal in answer.optionen:
        row = _option_row(
            proposal,
            issue=issue,
            material=material,
            position=len(rows) + 1,
            reference=reference,
            written_under=written_under,
        )
        if row is not None:
            rows.append(row)
    if len(rows) < RESPONSE_OPTIONS_MIN or not any(row.no_response for row in rows):
        _log.warning(
            "issue %d's options were not stored: %d survived and %s the silence "
            "among them; a set that can only propose acting is not offered",
            issue.id,
            len(rows),
            "there is" if any(row.no_response for row in rows) else "there is not",
        )
        return []
    _one_recommendation(rows)
    for row in rows:
        session.add(row)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        _log.warning(
            "a concurrent generation stored issue %d's options first; the rows "
            "that stand are kept",
            issue.id,
        )
        return stored_options(session, issue)
    return rows


def clear_options(session: Session, issue: Issue) -> int:
    """Take the issue's options off, so a person can ask again. Returns how many."""
    rows = stored_options(session, issue)
    for row in rows:
        session.delete(row)
    if rows:
        session.commit()
    return len(rows)


def recommendation(rows: list[ResponseOption]) -> ResponseOption | None:
    """The one option put forward, or ``None`` where the answer named none."""
    return next((row for row in rows if row.recommended), None)


__all__ = [
    "CONDITION_LABELS",
    "CONDITION_READERS",
    "KIND_LABELS",
    "OptionProposal",
    "OptionSet",
    "ScenarioProposal",
    "ScenarioSet",
    "check_triggers",
    "clear_options",
    "clear_scenarios",
    "fired_marks",
    "fired_triggers",
    "generate_options",
    "generate_scenarios",
    "recommendation",
    "stored_options",
    "stored_scenarios",
]
