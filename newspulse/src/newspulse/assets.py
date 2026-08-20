"""What a format is, and where a finished text lives.

Why this exists
---------------
RauteOS writes one kind of text well: a letter to a named journalist. Everything
else an agency delivers, the release, the statement, the Q&A, the briefing, the
talking points, the guest article, is still typed from scratch in Word while the
tool sits on the thesis, the evidence and the guide that the text should be built
from.

The obvious way to close that gap is to copy ``outreach.py`` six times. Within a
month there would be six modules that had drifted apart and six places to fix the
day the house style changed. So the first thing built here is not a format. It is
the shape a format has: what it needs before it may be written, what its output
must structurally contain, who it is attributed to, and how it is checked. Each
format is then a :class:`FormatDef` against that shape, and the seventh, whenever
somebody wants one, is a definition and a prompt file and no change to the writer.

Two rules carry over from the letter and are not negotiable
-----------------------------------------------------------
**Nothing is written from article bodies.** Only headlines, feed snippets and
what the profile holds. Upstream that is a schema guarantee, ``articles`` has no
body column at all, and :func:`prompt_for` keeps it visible: the evidence block is
built from ``title`` and ``summary_text`` and from nothing else.

**Nothing a required field would have supplied is invented.** DEC-2 locks the
refusal rule: a format declares its requirements, and if the profile names no
spokesperson then no statement is written and the reason names the field. Silence
is already an accepted answer in this codebase, :func:`newspulse.angles.suggest`
returns nothing and records why, and a fabricated quote attributed to a named CEO
is the single worst artefact this feature could produce. A thinly filled profile
therefore blocks formats, which is uncomfortable and correct.

Every finished text then passes both checks in :func:`check` before anyone reads
it: a second model on the text itself, and the client's own guide as a separate
verdict. An asset with neither recorded renders as unchecked, never as clean.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from importlib import resources
from string import Template

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, gemini, guide, profile, prose
from .analyzer import ParseError, invoke_with_fallback, strip_code_fence
from .models import Angle, Article, Asset, AssetKind, Client
from .schemas import AssetDraft, GuideVerdict, MessageReview

_log = logging.getLogger(__name__)

_CROSSCHECK_RESOURCE = "prompts/crosscheck.txt"

#: How many stories from the impulse travel into a format prompt. The angle
#: already rests on them and states its own context in prose; past this the
#: prompt stops being a brief and becomes a clippings service.
_MAX_EVIDENCE = 8

#: Ceiling on one feed snippet. A feed carries a sentence or two; anything longer
#: is a scraper's output, and this is the line where that would have to enter a
#: prompt. Cut here so no format can ever be written from an article body.
_MAX_SNIPPET_CHARS = 400

#: How many talking points are still talking points. Past this a consultant in a
#: green room is reading a document instead of remembering three things.
MAX_TALKING_POINTS = 5

#: Roughly what a German op-ed page holds. Stated to the model rather than
#: enforced here; the structural validators are FMT-02's.
GASTBEITRAG_CHARS = 4000

#: What a guest article needs under it before it may argue anything.
_MIN_GASTBEITRAG_EVIDENCE = 2


class Source(StrEnum):
    """Where a required input is read from.

    Three, because a mandate's facts live in three places that mean different
    things: the deep-dive profile a human filled, the mandate record itself, and
    the impulse this text argues.
    """

    PROFIL = "profil"
    MANDAT = "mandat"
    IMPULS = "impuls"


#: Labels for the non-profile requirements. The profile's own come from
#: :data:`newspulse.profile.FIELDS`, so a renamed field cannot leave a refusal
#: pointing at a name the page no longer shows.
_MANDATE_LABELS = {"comms_guide": "Kommunikations-Guide"}
_ANGLE_LABELS = {
    "thesis": "These des Impulses",
    "overclaim": "Die ausdrücklich nicht vertretene Lesart",
    "statements": "Ableitbare Aussagen",
    "article_ids": "Belegte Meldungen",
}

_WHERE = {
    Source.PROFIL: "im Profil",
    Source.MANDAT: "beim Mandanten",
    Source.IMPULS: "am Impuls",
}


@dataclass(frozen=True, slots=True)
class Requirement:
    """One thing a format needs before it may be written.

    Data rather than a predicate, so a refusal can name the field the consultant
    has to go and fill instead of saying that something was missing.
    """

    source: Source
    key: str
    #: How many entries a list-valued input needs. Text is present or it is not.
    minimum: int = 1

    @property
    def label(self) -> str:
        if self.source is Source.PROFIL:
            field = profile.FIELDS_BY_KEY.get(self.key)
            return field.label if field else self.key
        table = _MANDATE_LABELS if self.source is Source.MANDAT else _ANGLE_LABELS
        return table.get(self.key, self.key)

    @property
    def where(self) -> str:
        return _WHERE[self.source]


@dataclass(frozen=True, slots=True)
class Readiness:
    """Whether a format may be written for one mandate, and what is missing."""

    missing: tuple[Requirement, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.missing

    @property
    def reason(self) -> str:
        """The refusal, in a sentence that names the field to go and fill."""
        if self.ok:
            return ""
        named = [f"{req.label} ({req.where})" for req in self.missing]
        if len(named) == 1:
            return f"Es fehlt: {named[0]}."
        return f"Es fehlen: {', '.join(named[:-1])} und {named[-1]}."


@dataclass(frozen=True, slots=True)
class FormatDef:
    """One format, entirely as data.

    Everything that distinguishes a press release from a set of talking points
    sits in this object: what it is called, what it needs, what its output must
    structurally contain, and which file holds its prompt. :func:`write` reads
    all of it and knows none of it, which is what makes a seventh format a
    definition and a text file rather than a seventh code path.
    """

    #: The stored key. An :class:`AssetKind` for the six that exist, but typed
    #: loosely on purpose: this registry, not the enum, is what decides which
    #: formats there are, and a seventh must not need a schema change.
    kind: AssetKind | str
    #: The German name, as the consultant says it.
    name: str
    #: One line for the surface: what this format is for.
    description: str
    #: The prompt, as a package resource path.
    prompt: str
    #: What must be on file before this may be written at all.
    requires: tuple[Requirement, ...]
    #: What the output must carry. Travels into the prompt verbatim, so the
    #: contract is stated in exactly one place. FMT-02 validates against it.
    structure: tuple[str, ...]
    #: What the title field is called for this format, on the page and in the
    #: check. A release has a headline; talking points have neither.
    title_label: str = "Titel"
    #: Which profile field names the person a quote may be attributed to. Empty
    #: for the formats that quote nobody.
    speaker_key: str = ""

    @property
    def key(self) -> str:
        """The kind as it is stored and looked up.

        Normalised to a plain string in one place, so a definition written with
        an :class:`AssetKind` and one written with a bare key are stored, found
        and compared identically. Without it the two would work everywhere except
        the one place somebody wrote the other kind.
        """
        return str(self.kind)

    def template(self) -> Template:
        text = resources.files("newspulse").joinpath(self.prompt).read_text("utf-8")
        return Template(text)


FORMATS: tuple[FormatDef, ...] = (
    FormatDef(
        kind=AssetKind.PRESSEMITTEILUNG,
        name="Pressemitteilung",
        description="Die offizielle Meldung des Mandanten, zitierfähig und datiert.",
        prompt="prompts/pressemitteilung.txt",
        requires=(
            Requirement(Source.PROFIL, "ceo"),
            Requirement(Source.PROFIL, "geschaeftsfeld"),
        ),
        structure=(
            "Eine Schlagzeile, die die Nachricht enthält und nicht bewirbt.",
            "Eine Dateline: Ort, Datum.",
            "Ein Lead, der in einem Satz sagt, was passiert ist.",
            "Ein Fließtext, der ihn belegt.",
            "Genau ein Zitat, zugeschrieben an die oben genannte Person.",
            "Eine Boilerplate über den Mandanten am Ende.",
        ),
        title_label="Schlagzeile",
        speaker_key="ceo",
    ),
    FormatDef(
        kind=AssetKind.STATEMENT,
        name="Statement",
        description="Drei bis fünf zitierfähige Sätze einer namentlichen Person.",
        prompt="prompts/statement.txt",
        requires=(Requirement(Source.PROFIL, "ceo"),),
        structure=(
            "Drei bis fünf Sätze, so zitierfähig, wie sie gedruckt würden.",
            "Die Zuschreibung darunter: Name und Funktion.",
            "Kein Vorlauf, keine Einordnung, keine Anrede.",
        ),
        speaker_key="ceo",
    ),
    FormatDef(
        kind=AssetKind.QA,
        name="Q&A",
        description="Die Fragen, die kommen, samt der unangenehmen.",
        prompt="prompts/qa.txt",
        requires=(Requirement(Source.MANDAT, "comms_guide"),),
        structure=(
            "Fragen und Antworten, nach Themen gruppiert.",
            "Die unangenehmen Fragen ausdrücklich als solche markiert.",
            "Jede Antwort für sich sprechbar, ohne die vorherige.",
        ),
    ),
    FormatDef(
        kind=AssetKind.TALKING_POINTS,
        name="Talking Points",
        description="Was gesagt wird, was nicht, und der Weg zurück zur These.",
        prompt="prompts/talking_points.txt",
        requires=(
            Requirement(Source.IMPULS, "thesis"),
            Requirement(Source.IMPULS, "overclaim"),
        ),
        structure=(
            f"Höchstens {MAX_TALKING_POINTS} Punkte.",
            "Zu jedem Punkt eine Brücke zurück zur These.",
            'Ein eigener Abschnitt "Nicht sagen".',
        ),
    ),
    FormatDef(
        kind=AssetKind.GASTBEITRAG,
        name="Gastbeitrag",
        description="Ein argumentierter Text in der ersten Person, ohne Nachrichtenaufhänger.",
        prompt="prompts/gastbeitrag.txt",
        requires=(
            Requirement(Source.IMPULS, "thesis"),
            Requirement(
                Source.IMPULS, "article_ids", minimum=_MIN_GASTBEITRAG_EVIDENCE
            ),
        ),
        structure=(
            f"Rund {GASTBEITRAG_CHARS} Zeichen.",
            "Erste Person, ein Argument, das sich entwickelt.",
            "Kein Nachrichtenaufhänger, keine Dateline, kein Zitat von sich selbst.",
        ),
    ),
    FormatDef(
        kind=AssetKind.INTERVIEW_BRIEFING,
        name="Interview-Briefing",
        description="Wer fragt, was er zuletzt schrieb, und was gesagt werden soll.",
        prompt="prompts/interview_briefing.txt",
        requires=(Requirement(Source.IMPULS, "article_ids"),),
        structure=(
            "Wer fragt, und was diese Person zuletzt geschrieben hat.",
            "Was sie voraussichtlich fragen wird.",
            "Was der Mandant gesagt haben will, egal was gefragt wird.",
            "Wo es unangenehm wird.",
        ),
    ),
)

REGISTRY: dict[str, FormatDef] = {fmt.key: fmt for fmt in FORMATS}


def definition(kind: AssetKind | str) -> FormatDef:
    """The definition for one stored kind. Raises ``KeyError`` for an unknown one.

    Loud on an unknown kind by design: a stored text whose format nothing can
    describe is a text nothing can check, and rendering it as though it were fine
    is the failure worth avoiding.
    """
    return REGISTRY[str(kind)]


class RequirementsMissing(RuntimeError):
    """A format was asked for without what it needs.

    Raised rather than returned as an empty draft, because DEC-2 locks the rule
    as "write nothing and say which field is missing" and an exception is the one
    shape a caller cannot mistake for a text. The message names the fields, so
    the button that catches this has a sentence to show without composing one.
    """

    def __init__(self, fmt: FormatDef, readiness: Readiness) -> None:
        self.fmt = fmt
        self.readiness = readiness
        super().__init__(f"{fmt.name} nicht geschrieben. {readiness.reason}")

    @property
    def missing(self) -> tuple[Requirement, ...]:
        return self.readiness.missing


def requirements_met(
    session: Session,
    fmt: FormatDef,
    client: Client,
    angle: Angle | None = None,
) -> Readiness:
    """What this format is still missing for this mandate.

    ``angle`` may be ``None``: the surface asks which formats a mandate could
    write before an impulse is picked, and everything the impulse would supply is
    then honestly reported as missing rather than assumed.
    """
    facts = profile.stored(session, client.id)
    missing = tuple(
        req for req in fmt.requires if not _satisfied(req, facts, client, angle)
    )
    return Readiness(missing)


def _satisfied(req: Requirement, facts: dict, client: Client, angle: Angle | None) -> bool:
    if req.source is Source.PROFIL:
        fact = facts.get(req.key)
        return bool(fact and fact.value.strip())
    holder = client if req.source is Source.MANDAT else angle
    if holder is None:
        return False
    value = getattr(holder, req.key, None)
    if isinstance(value, list):
        return len(value) >= req.minimum
    return bool((value or "").strip())


# --- The prompt ----------------------------------------------------------------


def _client_profile(client: Client) -> str:
    parts = [f"Name: {client.name}"]
    if client.industry:
        parts.append(f"Branche: {client.industry}")
    if client.website:
        parts.append(f"Website: {client.website}")
    if client.keywords:
        parts.append(f"Themen: {', '.join(client.keywords)}")
    return "\n".join(parts)


def _facts_block(facts: dict) -> str:
    """The deep-dive profile, in the order the page reads it.

    Everything a format is allowed to state about the mandate as fact comes from
    here. A line that is not in this block is not a fact, it is a guess.
    """
    lines = [
        f"{field.label}: {facts[field.key].value}"
        for field in profile.FIELDS
        if field.key in facts and facts[field.key].value.strip()
    ]
    if not lines:
        return "Noch nichts hinterlegt. Behaupte also nichts über den Mandanten."
    return "\n".join(lines)


def _evidence_block(session: Session, angle: Angle) -> str:
    """The headlines and feed snippets under the impulse, and nothing else.

    This function is where the Leistungsschutzrecht rule would break if it were
    going to. It cannot: ``articles`` has no body column, so the two fields read
    here are the headline and the snippet the feed itself syndicated, and the
    snippet is cut at :data:`_MAX_SNIPPET_CHARS`.
    """
    ids = list(angle.article_ids or [])[:_MAX_EVIDENCE]
    if not ids:
        return (
            "BELEGTE MELDUNGEN\nKeine. Der Text darf sich also auf keine "
            "Berichterstattung berufen."
        )
    rows = session.scalars(select(Article).where(Article.id.in_(ids))).all()
    lines: list[str] = []
    for article in rows:
        lines.append(f"- ({article.source}) {article.title}")
        snippet = (article.summary_text or "").strip()
        if snippet:
            lines.append(f"  Feed-Anriss: {snippet[:_MAX_SNIPPET_CHARS]}")
    return (
        "BELEGTE MELDUNGEN\n"
        "Schlagzeilen und Feed-Anrisse, mehr war nie zu sehen. Was hier nicht "
        "steht, ist über diese Artikel nicht bekannt.\n" + "\n".join(lines)
    )


def _speaker(fmt: FormatDef, facts: dict) -> str:
    """The person a quote may be attributed to, exactly as the profile holds it."""
    if not fmt.speaker_key:
        return ""
    fact = facts.get(fmt.speaker_key)
    return fact.value.strip() if fact else ""


def _refusal_block(fmt: FormatDef) -> str:
    """The prompt's half of DEC-2.

    The code already refused before this prompt was built, so this is the second
    belt: it tells the model that the required facts are the ones above and that
    a plausible substitute for a missing one, including a bracketed placeholder,
    is worse than no text at all.
    """
    named = ", ".join(req.label for req in fmt.requires) or "keine Pflichtangaben"
    return (
        "WAS DU NICHT ERFINDEST\n"
        f"Dieses Format braucht: {named}. Was davon gebraucht wird, steht oben. "
        "Steht es dort nicht, erfindest du es nicht: keinen Namen, keine "
        "Funktion, keine Zahl, kein Datum, kein Zitat und auch keinen Platzhalter "
        "in eckigen Klammern. Ein Zitat, das eine namentlich genannte Person nie "
        "gesagt hat, ist der eine Fehler, der sich nicht mehr reparieren lässt."
    )


def prompt_for(
    session: Session, fmt: FormatDef, client: Client, angle: Angle
) -> str:
    """Render one format's prompt. Every format gets the same blocks.

    One shared slot set rather than one per format: a prompt file uses what it
    needs and ignores the rest, which is what lets a seventh format be a file
    somebody writes rather than a change here.
    """
    facts = profile.stored(session, client.id)
    return fmt.template().substitute(
        format_name=fmt.name,
        structure="\n".join(f"- {line}" for line in fmt.structure),
        refusal=_refusal_block(fmt),
        client_profile=_client_profile(client),
        profile_facts=_facts_block(facts),
        comms_guide=guide.for_prompt(client),
        speaker=_speaker(fmt, facts) or "Niemand benannt.",
        thesis=angle.thesis or "nicht ausformuliert",
        overclaim=angle.overclaim or "nicht ausformuliert",
        angle_message=angle.message,
        context=angle.context or "nicht ausformuliert",
        evidence=_evidence_block(session, angle),
    )


# --- Writing -------------------------------------------------------------------


def _parse(raw: str) -> AssetDraft:
    """Validate the reply into a draft; anything else is a ParseError."""
    try:
        payload = json.loads(strip_code_fence(raw))
    except json.JSONDecodeError as exc:
        raise ParseError(f"the format reply was not valid JSON: {exc}") from exc
    try:
        draft = AssetDraft.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 — pydantic raises its own type
        raise ParseError(f"the format reply did not match the schema: {exc}") from exc
    if not draft.body.strip():
        raise ParseError("the format reply carried no text")
    return draft


def write(
    session: Session,
    fmt: FormatDef,
    client: Client,
    angle: Angle,
    *,
    invoke: Callable[..., str] = invoke_with_fallback,
) -> AssetDraft:
    """Write ``fmt`` for this mandate off this impulse.

    Raises :class:`RequirementsMissing` before spending a model call when the
    format's declared inputs are not on file. That is DEC-2: nothing is written
    and the reason names the field.

    Note what is *not* here: no branch on which format this is. The definition
    carries the requirements, the structure and the prompt, so this function is
    the same code for the first format and for the seventh.

    ``invoke`` is injectable so the tests drive the whole path, prompt to stored
    row, without a subprocess.
    """
    readiness = requirements_met(session, fmt, client, angle)
    if not readiness.ok:
        _log.info("%s refused for %r: %s", fmt.key, client.name, readiness.reason)
        raise RequirementsMissing(fmt, readiness)
    prompt = prompt_for(session, fmt, client, angle)
    return _parse(invoke(prompt, timeout=config.ANALYZER_TIMEOUT))


# --- The checks, shared by every format ----------------------------------------


@dataclass(frozen=True, slots=True)
class Checkable:
    """A finished text plus everything the model that wrote it was allowed to see.

    The checkers take this rather than an ``Asset`` or an ``Outreach`` row, which
    is what lets the letter and the six formats go through the same code: what a
    check needs is a text, a position and the evidence under it, and all seven
    have those.
    """

    #: How to name this text to the checker, in the accusative: "eine
    #: Pressemitteilung", "ein Anschreiben an eine namentliche Journalistin".
    kind: str
    title_label: str
    title: str
    body: str
    thesis: str
    overclaim: str
    #: Everything that was provable, already rendered.
    evidence: str


@dataclass(frozen=True, slots=True)
class Checked:
    """Both verdicts on one text, and which model gave each.

    Every field can be empty, and an empty one means the check did not run. That
    distinction is the whole point of storing them separately: a text nothing has
    read must never render like a text nothing objected to.
    """

    review: MessageReview | None = None
    reviewed_by: str = ""
    guide: GuideVerdict | None = None
    guide_reviewed_by: str = ""
    #: Why the guide check did not run, when it did not.
    guide_note: str = ""


def crosscheck(
    client: Client,
    item: Checkable,
    *,
    generate: Callable[..., str] | None = None,
) -> tuple[MessageReview, str]:
    """Have a *different* model read the text, and say which one did.

    The model that wrote a text is the worst available judge of whether it
    oversells: it chose every word for a reason it still believes, and asking it
    to review its own work reliably produces "looks good". So this runs on the
    configured second provider and is asked one narrow question: would this
    embarrass the sender.

    Raises :class:`RuntimeError` when no second model is configured, because a
    check that silently did not happen is worse than no check at all.
    """
    if generate is None:
        generate = gemini.reviewer()

    template = Template(
        resources.files("newspulse").joinpath(_CROSSCHECK_RESOURCE).read_text("utf-8")
    )
    prompt = template.substitute(
        client=client.name,
        kind=item.kind,
        thesis=item.thesis or "nicht ausformuliert",
        overclaim=item.overclaim or "nicht ausformuliert",
        evidence=item.evidence,
        title_label=item.title_label,
        title=item.title,
        body=item.body,
    )
    raw = generate(prompt)
    try:
        payload = json.loads(strip_code_fence(raw))
        review = MessageReview.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 — pydantic and json raise their own
        raise ParseError(f"crosscheck did not match the schema: {exc}") from exc

    # One thing the checker cannot be trusted to catch, because it is mechanical:
    # the house rule on dashes. Checked here rather than believed.
    if prose.has_dash(item.body) or prose.has_dash(item.title):
        review = review.model_copy(
            update={
                "concerns": [
                    *review.concerns,
                    "Gedankenstrich im Text, verrät maschinelles Schreiben.",
                ][:5]
            }
        )
    return review, config.review_model()


def check(
    client: Client,
    item: Checkable,
    *,
    generate: Callable[..., str] | None = None,
    guide_generate: Callable[..., str] | None = None,
) -> Checked:
    """Both checks on one finished text: the second model, then the guide.

    The one entry point every format goes through, which is the point: six
    formats that each remembered to call a checker would be five formats and one
    that shipped unchecked in the week somebody was in a hurry.

    Two verdicts rather than one merged one. Invention and overclaiming are
    judgements about the world and the checker weighs them; a No-Go is not a
    judgement, the client wrote it down, so it is reported on its own and never
    averaged into a style note.
    """
    review, reviewed_by = crosscheck(client, item, generate=generate)
    verdict = guide.check(
        client,
        title=item.title,
        body=item.body,
        kind=item.kind,
        generate=guide_generate if guide_generate is not None else generate,
    )
    if verdict is None:
        return Checked(
            review=review, reviewed_by=reviewed_by, guide_note=guide.NO_GUIDE
        )
    guide_verdict, guide_reviewed_by = verdict
    return Checked(
        review=review,
        reviewed_by=reviewed_by,
        guide=guide_verdict,
        guide_reviewed_by=guide_reviewed_by,
    )


def checkable(
    session: Session, fmt: FormatDef, angle: Angle, draft: AssetDraft
) -> Checkable:
    """The finished draft, packed for :func:`check` with what backs it."""
    return Checkable(
        kind=f"ein Text im Format {fmt.name}",
        title_label=fmt.title_label,
        title=draft.title,
        body=draft.body,
        thesis=angle.thesis,
        overclaim=angle.overclaim,
        evidence=_evidence_block(session, angle),
    )


# --- Storage -------------------------------------------------------------------


def _apply_checks(row: Asset, checked: Checked | None) -> None:
    """Write both verdicts onto the row, or clear them.

    Clearing is the load-bearing half. A stored verdict always belongs to the
    text beside it, so re-writing a format drops the old one rather than letting
    it stand over a text it never read.
    """
    if checked is None or checked.review is None:
        row.review = ""
        row.reviewed_by = ""
        row.review_ok = True
    else:
        concerns = "\n".join(checked.review.concerns)
        if checked.review.fix:
            concerns = f"{concerns}\nZuerst ändern: {checked.review.fix}".strip()
        row.review = concerns
        row.reviewed_by = checked.reviewed_by
        row.review_ok = checked.review.send

    if checked is None or checked.guide is None:
        # A mandate with no guide leaves a note and no reviewer, so the page can
        # say the check could not run instead of showing an empty objection list.
        row.guide_review = checked.guide_note if checked else ""
        row.guide_reviewed_by = ""
        row.guide_review_ok = True
    else:
        row.guide_review = "\n".join(
            f"«{breach.sentence}» verstößt gegen: {breach.rule}"
            for breach in checked.guide.breaches
        )
        row.guide_reviewed_by = checked.guide_reviewed_by
        row.guide_review_ok = checked.guide.ok


def store(
    session: Session,
    fmt: FormatDef,
    client: Client,
    angle: Angle,
    draft: AssetDraft,
    checked: Checked | None = None,
) -> Asset:
    """Persist one generated text against the impulse it argues.

    Re-writing a format for the same impulse replaces the draft: two attempts at
    the same release are two attempts, not two releases. A *released* asset is
    never replaced, because its text is the record of what actually went out; a
    re-write becomes a new row beside it.
    """
    row = _replaceable(session, angle.id, fmt.key) or Asset(
        client_id=client.id, angle_id=angle.id, kind=fmt.key
    )
    # House style, enforced rather than requested: the prompts ask for no dashes
    # and the model relapses by the third paragraph. One call site for all seven
    # formats. See newspulse.prose.
    row.title = prose.plain(draft.title)
    row.body = prose.plain(draft.body)
    row.speaker = draft.speaker.strip()
    row.generated_at = dt.datetime.now(dt.UTC)
    # The model's words, freshly. Whatever a human had done to the previous draft
    # was done to a different text.
    row.edited_at = None
    _apply_checks(row, checked)
    session.add(row)
    session.commit()
    return row


def _replaceable(session: Session, angle_id: int, kind: str) -> Asset | None:
    """The draft this write would replace, if there is one that may be replaced."""
    return session.scalars(
        select(Asset)
        .where(
            Asset.angle_id == angle_id,
            Asset.kind == kind,
            Asset.released_at.is_(None),
        )
        .order_by(Asset.generated_at.desc(), Asset.id.desc())
    ).first()


def for_angle(session: Session, angle_id: int) -> list[Asset]:
    """Every text written off one impulse, newest first."""
    return list(
        session.scalars(
            select(Asset)
            .where(Asset.angle_id == angle_id)
            .order_by(Asset.generated_at.desc(), Asset.id.desc())
        ).all()
    )


def by_angle(session: Session, angle_ids: list[int]) -> dict[int, list[Asset]]:
    """The texts for several impulses at once, keyed by angle id.

    One query for the page rather than one per card, for the same reason the
    letters are fetched this way: the client view renders several impulses.
    """
    if not angle_ids:
        return {}
    grouped: dict[int, list[Asset]] = {}
    for row in session.scalars(
        select(Asset)
        .where(Asset.angle_id.in_(angle_ids))
        .order_by(Asset.generated_at.desc(), Asset.id.desc())
    ).all():
        grouped.setdefault(row.angle_id, []).append(row)
    return grouped


__all__ = [
    "FORMATS",
    "REGISTRY",
    "Checkable",
    "Checked",
    "FormatDef",
    "Readiness",
    "Requirement",
    "RequirementsMissing",
    "Source",
    "by_angle",
    "check",
    "checkable",
    "crosscheck",
    "definition",
    "for_angle",
    "prompt_for",
    "requirements_met",
    "store",
    "write",
]
