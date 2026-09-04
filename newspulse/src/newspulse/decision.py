"""Das Entscheidungspapier (RIS-05).

One page that makes a decision possible, and afterwards the document proving
what the decision rested on. The whole value is one separation held four ways:
what is **belegt**, what is **unbestätigt**, what is **offen**, and where two
stored lines **contradict** each other.

**A sentence is led as belegt only if it resolves to a stored row.** The prompt
shows the material with a Kennung on every line — ``[beitrag:12]``,
``[profil:4]`` — and :func:`_resolve` maps a cited Kennung back to the line it
was offered from, never to a row the model named out of nowhere. A sentence
whose Kennung resolves to nothing does not become a claim: it moves under
*unbestätigt*, where a reader can see that we have heard it and cannot stand it
up. That move is the acceptance itself, and ``test_decision.py`` exercises it
with an injected generation.

**Die Quellenordnung is fixed and printed on the paper.**
:class:`~newspulse.models.SourceRank` holds it in declaration order — a
confirmed internal statement, an authority or an original document, a verified
media report, everything else — and :data:`RANK_BY_KIND` is the one place an
evidence kind is mapped onto it. Every belegt sentence carries the rank of its
strongest line, so a reader deciding under pressure can see whether they are
acting on our own confirmed record or on a claim in somebody's inbox. The half
the model is told sits in :file:`blocks/quellenordnung.txt` and in no other
block: every prompt in the tree wants "nothing unbacked", and this is the only
one that prints Kennungen, so the instruction to name a line under each
supported sentence is a standard for one prompt rather than for twenty.

**A contradiction is reported only with both sides named.** A reported
contradiction whose second side nobody can name is worse than none at all,
because in a crisis it is believed. :func:`_contradiction_rows` drops a
contradiction whose either side fails to resolve, and both columns of
:class:`~newspulse.models.DecisionContradiction` are NOT NULL so no later writer
can store a half one.

**The gaps are the part a person under pressure does not assemble.** Three of
them are found in the stored material and frozen onto the paper; the decider and
the deadline are read live off the packet, because naming them is what gets them
filled in and the acceptance puts them at the *top* of the paper rather than
leaving a blank line. :func:`gaps` puts both origins into one list, each with
the link to where it is closed.

**The paper is stored as it read.** Statements, evidence and contradictions copy
their text rather than resolving pointers at render time, and a new paper for the
same issue stands *beside* the old one and never replaces it: afterwards, the
question asked is always what was known at the time.

The model call is injectable, and no test here exercises it against a real
backend.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from dataclasses import dataclass
from importlib import resources
from string import Template

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import assets as assets_mod
from . import brain, config, profile, prose, reporting
from .analyzer import ParseError, invoke_with_fallback, strip_code_fence
from .models import (
    DECISION_NAME_MAX,
    EVIDENCE_LABEL_MAX,
    Analysis,
    Article,
    Asset,
    Client,
    Crisis,
    DecisionContradiction,
    DecisionEvidence,
    DecisionGap,
    DecisionPacket,
    DecisionStatement,
    EvidenceKind,
    GapKind,
    Issue,
    MarketSignal,
    Outreach,
    OutreachReply,
    PacketSection,
    SourceRank,
)

_log = logging.getLogger(__name__)

_PROMPT = "prompts/decision_packet.txt"

#: How many lines of one kind the prompt shows, newest first. The paper exists
#: to make one decision possible, not to reprint the archive, and the same ten
#: :mod:`newspulse.scenarios` and :mod:`newspulse.stakeholders` show for the
#: same reason. The profile is exempt: it is a fixed handful of fields, and
#: cutting it would hide exactly the confirmed internal lines that rank highest.
_MAX_LINES_PER_KIND = 10

#: The profile fields whose absence the paper names as a gap, and the gap each
#: one is. Both are lines only a person can fill (``researched=False`` in
#: :data:`newspulse.profile.FIELDS`), which is why they are so often the two
#: missing on the evening they are needed.
_PROFILE_GAPS: dict[str, GapKind] = {
    "sprecher": GapKind.SPRECHER,
    "krisenkontakt": GapKind.KRISENKONTAKT,
}

#: Where each evidence kind sits in the Quellenordnung. The one place the
#: mapping exists: a profile field a person maintains and a text we released
#: are our own confirmed record; a market signal is an authority or an original
#: document; coverage and its analysis are a verified media report; a message
#: in the mailbox is somebody's claim and nothing more.
RANK_BY_KIND: dict[EvidenceKind, SourceRank] = {
    EvidenceKind.PROFIL: SourceRank.INTERN,
    EvidenceKind.TEXT: SourceRank.INTERN,
    EvidenceKind.MARKTSIGNAL: SourceRank.BEHOERDE,
    EvidenceKind.BEITRAG: SourceRank.MEDIEN,
    EvidenceKind.ANALYSE: SourceRank.MEDIEN,
    EvidenceKind.MAIL: SourceRank.UEBRIGES,
}

#: Every run of digits, which is what the figure rule compares. Deliberately
#: crude, and the same rule the scenarios take: a date, a sum, a count and a
#: percentage are all digits to a reader about to quote one, and "the material
#: did not carry this number" is the whole question. Written here rather than
#: imported, because the two features have to be able to say what *their* named
#: lines are without reaching into each other.
_DIGITS = re.compile(r"\d+")

#: How the prompt writes a Kennung, and how an answer is read back. Two parts
#: and nothing else: an answer that dresses one up ("Beitrag 12 (RP)") resolves
#: to nothing and its sentence lands under unbestätigt, which is the honest
#: outcome for a citation nobody can follow.
_TOKEN = re.compile(r"^\s*(?P<kind>[a-zäöüß]+)\s*:\s*(?P<ref>\d+)\s*$")


# --- The model's answer, as pydantic reads it --------------------------------------


class SupportedClaim(BaseModel):
    """One sentence the answer leads as supported, with the lines under it."""

    model_config = ConfigDict(extra="ignore")

    satz: str = ""
    beleg: list[str] = []


class PlainClaim(BaseModel):
    """One sentence that carries no evidence: unconfirmed, or an open question."""

    model_config = ConfigDict(extra="ignore")

    satz: str = ""


class ContradictionClaim(BaseModel):
    """One contradiction as the answer names it: what, and the two sides."""

    model_config = ConfigDict(extra="ignore")

    worin: str = ""
    seite_a: str = ""
    seite_b: str = ""


class PacketDraft(BaseModel):
    """The whole paper as the model returns it, before anything is resolved."""

    model_config = ConfigDict(extra="ignore")

    was_passiert_ist: str = ""
    belegt: list[SupportedClaim] = []
    unbestaetigt: list[PlainClaim] = []
    offen: list[PlainClaim] = []
    widersprueche: list[ContradictionClaim] = []
    zu_entscheiden: str = ""


# --- One line of the material ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Line:
    """One stored row as the paper may cite it.

    The Kennung (``kind`` plus ``ref_id``) is what the model quotes back and
    what the stored evidence keeps; ``label``, ``source``, ``happened_at`` and
    ``url`` are what gets copied onto the paper, so the paper still reads the
    same after the archive has moved on.
    """

    kind: EvidenceKind
    ref_id: int
    label: str
    source: str = ""
    happened_at: dt.datetime | None = None
    url: str = ""

    @property
    def token(self) -> str:
        """The Kennung as the prompt writes it and the answer cites it."""
        return f"{self.kind.value}:{self.ref_id}"

    @property
    def stated(self) -> str:
        """What this line actually says, which is what the figure rule measures.

        The Kennung is deliberately not part of it: an id is a number the tool
        printed, and counting it as material would let a model quote "12" back
        as a fact about the world.
        """
        when = f"{self.happened_at:%d.%m.%Y}" if self.happened_at else ""
        return " ".join(part for part in (when, self.source, self.label) if part)


def _shorten(text: str) -> str:
    """One label as it is stored: whitespace-flattened and cut to the column."""
    return " ".join((text or "").split())[:EVIDENCE_LABEL_MAX]


def _clean(text: str) -> str:
    """One line of generated prose as it is stored: dash-flattened and trimmed."""
    return prose.plain((text or "").strip())


def _anchor_issue(
    session: Session, *, issue: Issue | None, crisis: Crisis | None
) -> Issue | None:
    """The issue whose signals are this paper's coverage.

    For a crisis that is the issue it escalated out of, where there is one — the
    same handover :func:`newspulse.crisis.prehistory` reads, spelled here as its
    own query so this module owes the crisis module nothing.
    """
    if issue is not None:
        return issue
    if crisis is None:
        return None
    return session.scalars(
        select(Issue)
        .where(Issue.crisis_id == crisis.id)
        .order_by(Issue.opened_at.asc(), Issue.id.asc())
    ).first()


def _coverage_lines(
    session: Session,
    client: Client,
    *,
    anchor: Issue | None,
    crisis: Crisis | None,
) -> list[Line]:
    """The coverage and its analyses, newest first.

    The coverage comes off the occasion's own signals rather than off the day's
    archive: a paper about *this* matter that cited an unrelated headline from
    the same morning would put a reader one sentence away from the wrong story.
    A crisis declared cold has no issue behind it, so its own triggering piece
    is the whole of the coverage.
    """
    articles: dict[int, Article] = {}
    if anchor is not None:
        for row in sorted(anchor.signals, key=lambda s: s.happened_at, reverse=True):
            if row.article is not None:
                articles.setdefault(row.article.id, row.article)
    if crisis is not None and crisis.article is not None:
        articles.setdefault(crisis.article.id, crisis.article)
    kept = list(articles.values())[:_MAX_LINES_PER_KIND]
    lines = [
        Line(
            kind=EvidenceKind.BEITRAG,
            ref_id=row.id,
            label=_shorten(row.title),
            source=_shorten(row.source),
            happened_at=row.published_at,
            url=row.url,
        )
        for row in kept
    ]
    if not kept:
        return lines
    # The readings of exactly those pieces, for this mandate. A reading of the
    # same headline for another client says nothing about this one.
    readings = session.scalars(
        select(Analysis).where(
            Analysis.client_id == client.id,
            Analysis.article_id.in_([row.id for row in kept]),
        )
    ).all()
    by_article = {row.id: row for row in kept}
    for row in readings:
        summary = _shorten(row.summary or "")
        if not summary:
            continue
        piece = by_article.get(row.article_id)
        lines.append(
            Line(
                kind=EvidenceKind.ANALYSE,
                ref_id=row.id,
                label=summary,
                source=_shorten(
                    f"Analyse, {row.category.value}, Wichtigkeit "
                    f"{row.importance_score}"
                ),
                happened_at=piece.published_at if piece is not None else None,
            )
        )
    return lines


def _profile_lines(session: Session, client: Client) -> list[Line]:
    """Every stored profile field, in the order the profile is read.

    Not capped: the profile is a fixed handful of fields, and these are the
    highest-ranking lines the paper has — cutting them would hide exactly the
    confirmed internal statements a decision most wants to rest on.
    """
    facts = profile.stored(session, client.id)
    lines: list[Line] = []
    for field in profile.FIELDS:
        row = facts.get(field.key)
        if row is None or not (row.value or "").strip():
            continue
        lines.append(
            Line(
                kind=EvidenceKind.PROFIL,
                ref_id=row.id,
                label=_shorten(row.value),
                source=_shorten(f"Profil, {field.label}"),
                happened_at=row.updated_at,
            )
        )
    return lines


def _market_lines(anchor: Issue | None) -> list[Line]:
    """The market signals hanging on the occasion, newest first."""
    if anchor is None:
        return []
    rows: list[MarketSignal] = []
    for row in sorted(anchor.signals, key=lambda s: s.happened_at, reverse=True):
        if row.market_signal is not None:
            rows.append(row.market_signal)
    return [
        Line(
            kind=EvidenceKind.MARKTSIGNAL,
            ref_id=row.id,
            label=_shorten(row.title),
            source=_shorten(row.publisher or row.kind.value),
            happened_at=row.effective_at or row.published_at or row.found_at,
            url=row.url,
        )
        for row in rows[:_MAX_LINES_PER_KIND]
    ]


def _mail_lines(session: Session, client: Client, *, since: dt.datetime) -> list[Line]:
    """Replies in the connected mailbox since the matter began, newest first.

    Bounded by ``since`` for the same reason the scenario trigger is: a reply
    from before the matter started answers a different letter, and putting it on
    this paper would offer a reader evidence about something else.
    """
    rows = session.scalars(
        select(OutreachReply)
        .join(Outreach, Outreach.id == OutreachReply.outreach_id)
        .where(Outreach.client_id == client.id, OutreachReply.received_at >= since)
        .order_by(OutreachReply.received_at.desc())
        .limit(_MAX_LINES_PER_KIND)
    ).all()
    return [
        Line(
            kind=EvidenceKind.MAIL,
            ref_id=row.id,
            label=_shorten(row.body) or _shorten(row.letter.subject),
            source=_shorten(row.sender or row.letter.outlet),
            happened_at=row.received_at,
        )
        for row in rows
        if _shorten(row.body) or _shorten(row.letter.subject)
    ]


def _text_lines(session: Session, client: Client) -> list[Line]:
    """Texts of ours a person released, newest first.

    A released text is our own confirmed statement, which is why it ranks with
    the profile — and why it is the side a contradiction most often has: what we
    said last week against what is being claimed today.
    """
    rows = session.scalars(
        select(Asset)
        .where(Asset.client_id == client.id, Asset.released_at.is_not(None))
        .order_by(Asset.released_at.desc())
        .limit(_MAX_LINES_PER_KIND)
    ).all()
    lines: list[Line] = []
    for row in rows:
        try:
            format_name = assets_mod.definition(row.kind).name
        except KeyError:
            # A stored row whose format the registry no longer knows. Its kind
            # is still a true statement about it, and dropping the line would
            # take a released text of ours off the paper it belongs on.
            format_name = row.kind
        label = _shorten(row.title) or _shorten(row.body)
        if not label:
            continue
        lines.append(
            Line(
                kind=EvidenceKind.TEXT,
                ref_id=row.id,
                label=label,
                source=_shorten(f"{format_name}, freigegeben"),
                happened_at=row.released_at,
            )
        )
    return lines


def material_lines(
    session: Session,
    client: Client,
    *,
    issue: Issue | None = None,
    crisis: Crisis | None = None,
) -> list[Line]:
    """Every stored row this paper may cite, in the order the prompt lists them.

    The order is the Quellenordnung's, highest first, so a model reading top to
    bottom meets the confirmed internal lines before the coverage. Exposed
    because it is also what the page's "what could this have rested on" reading
    is, and because a test that cannot see the offered material cannot check
    that a Kennung outside it resolves to nothing.
    """
    anchor = _anchor_issue(session, issue=issue, crisis=crisis)
    began = _began_at(anchor=anchor, crisis=crisis)
    return [
        *_profile_lines(session, client),
        *_text_lines(session, client),
        *_market_lines(anchor),
        *_coverage_lines(session, client, anchor=anchor, crisis=crisis),
        *_mail_lines(session, client, since=began),
    ]


def _began_at(*, anchor: Issue | None, crisis: Crisis | None) -> dt.datetime:
    """When the matter began — the floor for what mail belongs on the paper."""
    if anchor is not None and crisis is not None:
        return min(anchor.opened_at, crisis.declared_at)
    if anchor is not None:
        return anchor.opened_at
    if crisis is not None:
        return crisis.declared_at
    return dt.datetime.min.replace(tzinfo=dt.UTC)


def _occasion_line(*, issue: Issue | None, crisis: Crisis | None) -> str:
    """The matter in one line: what the paper is about, from stored columns."""
    if issue is not None:
        line = issue.title
        if (issue.description or "").strip():
            line += f", {issue.description.strip()}"
        return line
    if crisis is not None:
        headline = crisis.article.title if crisis.article is not None else ""
        line = f"Krise, Stufe {crisis.level}, erklärt am {crisis.declared_at:%d.%m.%Y}"
        return f"{line}: {headline}" if headline else line
    return ""


def _material_block(lines: list[Line]) -> str:
    """The material as the prompt shows it: one Kennung per line, and the line."""
    rendered = [f"[{line.token}] {line.stated}" for line in lines]
    return "\n".join(rendered) or "Keine gespeicherten Zeilen."


def _figure_material(lines: list[Line], occasion: str) -> str:
    """What a figure in the generated prose is measured against.

    Everything the named lines actually say, and the matter's own line — but
    never the Kennungen, which are numbers this tool printed rather than facts
    anybody stated.
    """
    return "\n".join([occasion, *(line.stated for line in lines)])


def _unsupported_figures(text: str, material: str) -> list[str]:
    """Digit runs in ``text`` that ``material`` does not carry, in order.

    Compared as strings rather than as values, the same way the scenarios do it:
    a reader quoting "1,2 Millionen" back at a client quotes the characters, and
    a normalisation that made 1.200.000 and 1,2 the same figure would let one of
    them through under the other's authority.
    """
    supported = set(_DIGITS.findall(material))
    return [figure for figure in _DIGITS.findall(text or "") if figure not in supported]


def _unusable(text: str, material: str) -> str:
    """Why this sentence may not stand on the paper, or the empty string.

    Two rules and one place. A sentence carrying a figure the material does not
    hold is refused — an invented number on a decision paper is quoted back as a
    measurement — and so is one naming a figure this tool may not produce at all
    (:func:`newspulse.reporting.forbidden_terms`: reach, impressions, advertising
    value). Both rules read the same wherever the sentence stands: the paper's
    opening, the question somebody acts on and a contradiction's sentence are
    read as closely as its bullets are, and a check that held for the bullets
    alone would let the loudest line through.

    A reason rather than a bool, because the caller owes its log the word it
    tripped on: "names 'reichweite'" tells a reader in one line what to reword.
    """
    invented = _unsupported_figures(text, material)
    if invented:
        return f"figure(s) {invented} that stand in no named line"
    forbidden = reporting.forbidden_terms(text)
    if forbidden:
        return f"{list(forbidden)}, which this tool may not produce"
    return ""


# --- Resolving a Kennung -----------------------------------------------------------


def _by_token(lines: list[Line]) -> dict[str, Line]:
    """The offered material, keyed by the Kennung the prompt printed."""
    return {line.token: line for line in lines}


def _resolve(raw: str, offered: dict[str, Line]) -> Line | None:
    """The line a cited Kennung stands for, or ``None``.

    Resolution is against *what was offered*, never against the tables. That is
    what makes client scoping a property of this function rather than a rule
    every caller has to remember: a Kennung naming another mandate's row was
    never in the prompt, so it resolves to nothing here and its sentence lands
    under unbestätigt.
    """
    match = _TOKEN.match(str(raw or "").casefold())
    if match is None:
        return None
    return offered.get(f"{match.group('kind')}:{int(match.group('ref'))}")


def _evidence_rows(
    claim: SupportedClaim, offered: dict[str, Line]
) -> list[DecisionEvidence]:
    """The lines under one claim, each cited Kennung resolved once."""
    rows: list[DecisionEvidence] = []
    seen: set[str] = set()
    for raw in claim.beleg:
        line = _resolve(raw, offered)
        if line is None or line.token in seen:
            continue
        seen.add(line.token)
        rows.append(
            DecisionEvidence(
                kind=line.kind,
                ref_id=line.ref_id,
                label=line.label,
                source=line.source,
                happened_at=line.happened_at,
                url=line.url,
            )
        )
    return rows


def _rank_for(rows: list[DecisionEvidence]) -> SourceRank:
    """Where the strongest line under a sentence sits in the Quellenordnung.

    The strongest, not the first named: a sentence resting on a confirmed
    internal line *and* on a media report rests on the internal one, and showing
    it under the weaker rank would understate what the reader is standing on.
    """
    order = {rank: place for place, rank in enumerate(SourceRank)}
    return min((RANK_BY_KIND[row.kind] for row in rows), key=lambda rank: order[rank])


# --- Turning the answer into rows --------------------------------------------------


def _statement_rows(
    draft: PacketDraft, *, offered: dict[str, Line], material: str
) -> list[DecisionStatement]:
    """Every sentence of the paper as it is filed, in reading order.

    Two rules decide, and both are drops rather than repairs. A sentence
    carrying a figure the material does not hold is dropped outright — an
    invented number on a decision paper is quoted as a measurement — and so is
    one naming a figure this tool may not produce at all
    (:func:`newspulse.reporting.is_forbidden`: reach, impressions, advertising
    value). The third rule is not a drop: a supported sentence whose Kennungen
    all fail to resolve moves to *unbestätigt*, because "we have heard this and
    cannot stand it up" is worth reading.
    """
    rows: list[DecisionStatement] = []

    def _add(text: str, section: PacketSection, evidence: list[DecisionEvidence]) -> None:
        sentence = _clean(text)
        if not sentence:
            return
        refused = _unusable(sentence, material)
        if refused:
            _log.warning(
                "a sentence of the packet carries %s; it is dropped rather than "
                "put on a paper somebody decides from",
                refused,
            )
            return
        rows.append(
            DecisionStatement(
                section=section,
                text=sentence,
                source_rank=_rank_for(evidence) if evidence else None,
                position=len(rows) + 1,
                evidence=evidence,
            )
        )

    for claim in draft.belegt:
        evidence = _evidence_rows(claim, offered)
        if not evidence:
            _log.info(
                "a sentence led as belegt resolved to no stored line; it stands "
                "under unbestätigt rather than as a claim"
            )
        _add(
            claim.satz,
            PacketSection.BELEGT if evidence else PacketSection.UNBESTAETIGT,
            evidence,
        )
    for claim in draft.unbestaetigt:
        _add(claim.satz, PacketSection.UNBESTAETIGT, [])
    for claim in draft.offen:
        _add(claim.satz, PacketSection.OFFEN, [])
    return rows


def _contradiction_refusal(note: str, left: Line | None, right: Line | None) -> str:
    """Why a reported contradiction is not stored, or the empty string.

    Three causes and three sentences, because the one thing a reader looking for
    a missing contradiction needs is which of them fired: "it named two sides"
    is true of a self-contradiction and of one with no sentence, and reads as
    the opposite of the reason it was dropped.
    """
    if not note:
        return "it said nothing about what the contradiction is"
    named = sum(side is not None for side in (left, right))
    if named < 2:
        return f"only {named} of its two sides resolve to a stored line"
    if left is not None and right is not None and left.token == right.token:
        return f"both of its sides are the same line ({left.token})"
    return ""


def _contradiction_rows(
    draft: PacketDraft, *, offered: dict[str, Line], material: str
) -> list[DecisionContradiction]:
    """The contradictions with **both** sides named, in the order they came.

    A contradiction whose either side fails to resolve is not stored and not
    shown: a reported contradiction that cannot say what it contradicts is
    believed in a crisis, and that is worse than reporting none. The two sides
    must also be two different lines — a row contradicting itself is a reading
    error dressed as a finding.
    """
    rows: list[DecisionContradiction] = []
    for claim in draft.widersprueche:
        note = _clean(claim.worin)
        left = _resolve(claim.seite_a, offered)
        right = _resolve(claim.seite_b, offered)
        refusal = _contradiction_refusal(note, left, right)
        if refusal:
            _log.info("a contradiction was not stored: %s", refusal)
            continue
        refused = _unusable(note, material)
        if refused:
            _log.warning("a contradiction carries %s; it is dropped", refused)
            continue
        rows.append(
            DecisionContradiction(
                note=note,
                left_kind=left.kind,
                left_ref_id=left.ref_id,
                left_label=left.label,
                left_source=left.source,
                right_kind=right.kind,
                right_ref_id=right.ref_id,
                right_label=right.label,
                right_source=right.source,
                position=len(rows) + 1,
            )
        )
    return rows


def _material_gaps(
    session: Session, client: Client, statements: list[DecisionStatement]
) -> list[DecisionGap]:
    """The gaps this paper found in the material, frozen onto it.

    Three, and each is a mechanical reading of stored rows rather than something
    the model was asked to notice: an invented gap sends somebody looking for a
    field that does not exist.

    ``BETROFFENENZAHL`` is the one worth spelling out: a confirmed figure of
    those affected can only come from a *confirmed internal* line, so the gap
    holds exactly when no belegt sentence of rank ``INTERN`` carries a figure at
    all. That is the honest form of "no number on this paper is confirmed by
    us", and it is checkable in the hour somebody checks it.
    """
    facts = profile.stored(session, client.id)
    found: list[GapKind] = []
    for key, gap in _PROFILE_GAPS.items():
        row = facts.get(key)
        if row is None or not (row.value or "").strip():
            found.append(gap)
    confirmed = any(
        row.source_rank is SourceRank.INTERN and _DIGITS.search(row.text)
        for row in statements
    )
    if not confirmed:
        found.append(GapKind.BETROFFENENZAHL)
    return [
        DecisionGap(kind=kind, position=place)
        for place, kind in enumerate(found, start=1)
    ]


# --- The button --------------------------------------------------------------------


def _template() -> Template:
    text = resources.files("newspulse").joinpath(_PROMPT).read_text("utf-8")
    return Template(brain.compose(text))


def _ask(
    session: Session,
    client: Client,
    *,
    lines: list[Line],
    issue: Issue | None,
    crisis: Crisis | None,
    invoke,
) -> tuple[PacketDraft, int]:
    """Compose the prompt, spend the one call, and read the answer back.

    The standards version is captured *here*, when the prompt is composed, and
    not when the rows are saved: an edit landing while the model writes belongs
    to the next paper and not to this one.
    """
    written_under = brain.version(session)
    prompt = _template().substitute(
        client_name=client.name,
        occasion=_occasion_line(issue=issue, crisis=crisis) or "Keine Angabe gespeichert.",
        material=_material_block(lines),
    )
    resolved_invoke = invoke if invoke is not None else invoke_with_fallback
    return _parse(resolved_invoke(prompt, timeout=config.ANALYZER_TIMEOUT)), written_under


def _opening(draft: PacketDraft, *, material: str, client_id: int) -> str:
    """The paper's first paragraph, or the empty string where there is none.

    Empty means nothing is stored at all. A paper that cannot say what happened
    is not a paper, and one whose opening carries a figure the material never
    held is worse than none: it is the sentence a reader takes into the room.
    """
    situation = _clean(draft.was_passiert_ist)
    if not situation:
        _log.warning(
            "no decision packet was stored for client %d: the answer did not say "
            "what happened",
            client_id,
        )
        return ""
    refused = _unusable(situation, material)
    if refused:
        # Returned here rather than falling through: the answer *did* say what
        # happened, and a second line saying it did not would send the next
        # reader of the log looking for an empty answer that never arrived.
        _log.warning(
            "the packet's opening carries %s; the paper is not stored for "
            "client %d",
            refused,
            client_id,
        )
        return ""
    return situation


def build(
    session: Session,
    client: Client,
    *,
    issue: Issue | None = None,
    crisis: Crisis | None = None,
    by: str,
    invoke=None,
    now: dt.datetime | None = None,
) -> DecisionPacket | None:
    """Write one decision paper to an issue or to a crisis. Never replaces one.

    Deliberately **not** idempotent, which is the opposite of what the scenarios
    do and is the acceptance here: "ein neues Papier zum selben Issue ersetzt das
    alte nicht, sondern tritt daneben". Two papers a week apart are the record of
    how the reading changed, and that is the thing asked about afterwards.

    ``None`` comes back where the answer could not say what happened. A paper
    that cannot open with the situation is not a paper, and storing one would put
    a page headed by nothing in front of a reader who is about to decide from it.
    """
    if (issue is None) == (crisis is None):
        raise ValueError("a decision packet hangs on exactly one occasion")
    lines = material_lines(session, client, issue=issue, crisis=crisis)
    material = _figure_material(lines, _occasion_line(issue=issue, crisis=crisis))
    draft, written_under = _ask(
        session, client, lines=lines, issue=issue, crisis=crisis, invoke=invoke
    )
    situation = _opening(draft, material=material, client_id=client.id)
    if not situation:
        return None

    offered = _by_token(lines)
    statements = _statement_rows(draft, offered=offered, material=material)
    question = _clean(draft.zu_entscheiden)
    refused = _unusable(question, material) if question else ""
    if refused:
        # Dropped rather than stored: "was jetzt zu entscheiden ist" is the one
        # sentence on the paper somebody acts on, and it is held to the same two
        # rules as every bullet under it.
        _log.warning("the packet's question carries %s; it is dropped", refused)
        question = ""
    packet = DecisionPacket(
        client_id=client.id,
        issue_id=issue.id if issue is not None else None,
        crisis_id=crisis.id if crisis is not None else None,
        situation=situation,
        question=question,
        created_at=now or dt.datetime.now(dt.UTC),
        created_by=(by or "").strip()[:DECISION_NAME_MAX] or "mensch",
        brain_version=brain.stamp(written_under, what="a decision packet"),
        statements=statements,
        contradictions=_contradiction_rows(draft, offered=offered, material=material),
        stored_gaps=_material_gaps(session, client, statements),
    )
    session.add(packet)
    session.commit()
    _log.info(
        "decision packet %d written for client %d: %d statement(s), %d "
        "contradiction(s)",
        packet.id,
        client.id,
        len(packet.statements),
        len(packet.contradictions),
    )
    return packet


def _parse(raw: str) -> PacketDraft:
    """The payload out of the model's answer, or :class:`ParseError`."""
    try:
        payload = json.loads(strip_code_fence(raw))
    except json.JSONDecodeError as exc:
        raise ParseError(f"decision packet answer was not valid JSON: {exc}") from exc
    try:
        return PacketDraft.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 — pydantic raises its own type
        raise ParseError(
            f"decision packet answer did not match the schema: {exc}"
        ) from exc


# --- Reading the papers back -------------------------------------------------------


#: How a list of papers is ordered, wherever one is read: newest first, ties
#: broken by id. One tuple rather than one order_by per reader — the register
#: reads every row's papers in a single query and the occasion pages read one
#: occasion's, and two lists of the same papers must not come back differently
#: sorted on two pages.
NEWEST_FIRST = (DecisionPacket.created_at.desc(), DecisionPacket.id.desc())


def packets_for(
    session: Session, *, issue: Issue | None = None, crisis: Crisis | None = None
) -> list[DecisionPacket]:
    """The papers written to one occasion, newest first.

    Newest first because that is the one somebody is working from; the older
    ones stand beside it, which is the whole point of never replacing them.
    """
    if (issue is None) == (crisis is None):
        raise ValueError("ask for one occasion's papers, not both or neither")
    query = select(DecisionPacket).order_by(*NEWEST_FIRST)
    if issue is not None:
        query = query.where(DecisionPacket.issue_id == issue.id)
    else:
        query = query.where(DecisionPacket.crisis_id == crisis.id)
    return list(session.scalars(query).all())


def anchor_issue(session: Session, row: DecisionPacket) -> Issue | None:
    """The issue whose stored rows this paper's occasion is made of.

    The paper's own issue where it has one, and the issue a crisis escalated out
    of otherwise. It is what the page reads the response options off: the
    options hang on the *matter*, and a crisis that grew out of an issue is the
    same matter under a different name.
    """
    if row.issue_id is not None:
        return session.get(Issue, row.issue_id)
    standing = session.get(Crisis, row.crisis_id) if row.crisis_id else None
    if standing is None:
        return None
    return _anchor_issue(session, issue=None, crisis=standing)


def occasion(session: Session, row: DecisionPacket) -> str:
    """The matter this paper was written to, in one line.

    Read off the anchor rather than copied onto the packet: the issue's title is
    the matter's name, and a paper that kept a stale copy of it would head a
    document with a name nobody uses any more.
    """
    if row.issue_id is not None:
        return _occasion_line(issue=session.get(Issue, row.issue_id), crisis=None)
    standing = session.get(Crisis, row.crisis_id) if row.crisis_id else None
    return _occasion_line(issue=None, crisis=standing)


def packet(session: Session, client: Client, packet_id: int) -> DecisionPacket | None:
    """One paper of this mandate, or ``None``.

    Scoped to the mandate rather than fetched by id alone: the paper carries a
    company's unconfirmed claims and its contradictions, and a hand-typed id
    must not be able to read another mandate's.
    """
    return session.scalars(
        select(DecisionPacket).where(
            DecisionPacket.id == packet_id, DecisionPacket.client_id == client.id
        )
    ).first()


def sections(row: DecisionPacket) -> dict[PacketSection, list[DecisionStatement]]:
    """The paper's sentences grouped by part, every part present.

    Every part, including the empty ones: the separation is the value, and a
    part that vanished when it held nothing would leave a reader unable to tell
    "nothing is unconfirmed" from "nobody looked".
    """
    grouped: dict[PacketSection, list[DecisionStatement]] = {
        section: [] for section in PacketSection
    }
    for statement in sorted(row.statements, key=lambda s: s.position):
        grouped[statement.section].append(statement)
    return grouped


@dataclass(frozen=True, slots=True)
class Gap:
    """One named gap on the paper, with the link to where it is closed."""

    kind: GapKind
    #: The sentence a reader acts on, and a key in the i18n table.
    label: str
    #: Where it is filled in. Empty in the downloaded paper, which carries no
    #: link back into this application.
    link: str
    #: Whether it belongs at the *top* of the paper. The decider and the
    #: deadline do: "fehlen Entscheider oder Frist, steht das oben auf dem
    #: Papier und nicht als Leerstelle".
    leading: bool = False


#: What each gap is called where a person reads it. Here rather than in the
#: template because every one is a key in the i18n table, and a sentence
#: assembled in Jinja cannot be looked up — it would render German on an
#: English page.
GAP_LABELS: dict[GapKind, str] = {
    GapKind.SPRECHER: "Im Profil steht kein Sprecher.",
    GapKind.KRISENKONTAKT: "Im Profil steht kein Krisenkontakt.",
    GapKind.BETROFFENENZAHL: (
        "Keine bestätigte Zahl auf diesem Papier: keine belegte Angabe aus "
        "einer bestätigten internen Quelle nennt eine Zahl."
    ),
    GapKind.ENTSCHEIDER: "Auf diesem Papier steht kein Entscheider.",
    GapKind.FRIST: "Auf diesem Papier steht keine Frist.",
}

#: What each part of the paper is called. Lookup keys for the same reason.
SECTION_LABELS: dict[PacketSection, str] = {
    PacketSection.BELEGT: "Belegt",
    PacketSection.UNBESTAETIGT: "Unbestätigt",
    PacketSection.OFFEN: "Offen",
}

#: What each kind of cited line is called beside its Kennung.
EVIDENCE_LABELS: dict[EvidenceKind, str] = {
    EvidenceKind.BEITRAG: "Beitrag",
    EvidenceKind.ANALYSE: "Analyse",
    EvidenceKind.PROFIL: "Profilfeld",
    EvidenceKind.MARKTSIGNAL: "Marktsignal",
    EvidenceKind.MAIL: "Mail",
    EvidenceKind.TEXT: "Freigegebener Text",
}


def gaps(session: Session, row: DecisionPacket) -> list[Gap]:
    """Every named gap on this paper, the leading ones first.

    Two origins, one list. The material gaps were frozen when the paper was
    written, because the paper is the record of what was known then. The decider
    and the deadline are read live off the packet: naming them is what gets them
    filled in, and a frozen row would keep reporting them missing afterwards.
    """
    profile_link = f"/client/{row.client_id}/profil"
    packet_link = f"/client/{row.client_id}/entscheidungspapier/{row.id}"
    leading: list[Gap] = []
    if not row.decision_maker.strip():
        leading.append(
            Gap(
                kind=GapKind.ENTSCHEIDER,
                label=GAP_LABELS[GapKind.ENTSCHEIDER],
                link=packet_link,
                leading=True,
            )
        )
    if row.deadline is None:
        leading.append(
            Gap(
                kind=GapKind.FRIST,
                label=GAP_LABELS[GapKind.FRIST],
                link=packet_link,
                leading=True,
            )
        )
    stored = [
        Gap(kind=gap.kind, label=GAP_LABELS[gap.kind], link=profile_link)
        for gap in sorted(row.stored_gaps, key=lambda g: g.position)
    ]
    return [*leading, *stored]


# --- What a person writes on the paper ---------------------------------------------


def set_decider(
    session: Session,
    row: DecisionPacket,
    *,
    decision_maker: str = "",
    deadline: dt.datetime | None = None,
) -> bool:
    """Record who decides and by when. ``False`` where the paper is already closed.

    Both are a person's to set, never the tool's: a decider this tool nominated
    would be a name nobody agreed to, and a deadline it computed would be a
    promise nobody made. A decided paper refuses the write — it is the record of
    what a decision rested on, and editing it afterwards is the one thing that
    would make it worthless.
    """
    if row.is_decided:
        return False
    row.decision_maker = (decision_maker or "").strip()[:DECISION_NAME_MAX]
    row.deadline = deadline
    session.commit()
    return True


def record_decision(
    session: Session,
    row: DecisionPacket,
    *,
    decision: str,
    by: str,
    now: dt.datetime | None = None,
) -> bool:
    """Note the decision that was taken and who took it. ``False`` where refused.

    Refused for an empty decision and for a paper that already carries one: the
    three columns are what makes the paper answer the question asked afterwards,
    and a second decision written over the first would erase the answer rather
    than add to it. A changed mind is a new paper, which is what
    :func:`build` is for.
    """
    words = _clean(decision)
    if not words or row.is_decided:
        return False
    row.decision = words
    row.decided_by = (by or "").strip()[:DECISION_NAME_MAX] or "mensch"
    row.decided_at = now or dt.datetime.now(dt.UTC)
    session.commit()
    return True


__all__ = [
    "EVIDENCE_LABELS",
    "GAP_LABELS",
    "NEWEST_FIRST",
    "Gap",
    "Line",
    "PacketDraft",
    "RANK_BY_KIND",
    "SECTION_LABELS",
    "anchor_issue",
    "build",
    "gaps",
    "material_lines",
    "occasion",
    "packet",
    "packets_for",
    "record_decision",
    "sections",
    "set_decider",
]
