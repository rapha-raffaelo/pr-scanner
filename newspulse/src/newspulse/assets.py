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

**Nothing is stored that does not carry its own shape.** Each format declares
what its output must contain and a validator that says whether it does, run on
the model's answer before storage. This is where the PR craft is held: a press
release without an attributed quote, a set of talking points with nine points and
no bridges, a guest article that opens with a dateline. Each is a text a
consultant has to read to the end to discover is unusable, and each is
mechanically detectable in a millisecond. A miss buys one retry carrying the
complaint, the analyzer's budget for a batch, and then a refusal that names what
was missing rather than a stored draft nobody can send.

Every finished text then passes both checks in :func:`check` before anyone reads
it: a second model on the text itself, and the client's own guide as a separate
verdict. An asset with neither recorded renders as unchecked, never as clean.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from importlib import resources
from string import Template

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import brain, config, gemini, guide, pitch, profile, prose
from .analyzer import AnalyzerError, ParseError, invoke_with_fallback, strip_code_fence
from .models import Angle, Article, Asset, AssetKind, CheckState, Client, ClientFact
from .pitch import PitchTarget
from .schemas import (
    MAX_CONCERNS,
    AssetDraft,
    GuideVerdict,
    MessageReview,
    without_provenance,
)

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

#: How far a guest article may miss that target before it is a different text. An
#: op-ed page holds what it holds: at half the length it is a column, at half again
#: it is an essay, and neither is what was commissioned.
_GASTBEITRAG_TOLERANCE = 0.4
_MIN_GASTBEITRAG_CHARS = int(GASTBEITRAG_CHARS * (1 - _GASTBEITRAG_TOLERANCE))
_MAX_GASTBEITRAG_CHARS = int(GASTBEITRAG_CHARS * (1 + _GASTBEITRAG_TOLERANCE))

#: A statement is what a desk prints whole. Under three sentences it says nothing
#: worth attributing; over five a subeditor picks two and the mandate finds out
#: afterwards which ones.
_MIN_STATEMENT_SENTENCES = 3
_MAX_STATEMENT_SENTENCES = 5

#: Lead, body and boilerplate are three things, so a release is at least three
#: paragraphs. Fewer means they have been run together, and a desk that cannot see
#: where the boilerplate starts prints it as part of the news.
_MIN_RELEASE_PARAGRAPHS = 3

#: How many quotes a release carries. One, because a desk lifts the quote whole
#: and a second voice is a second person to have cleared it. Written out in words
#: in the structure line the prompt shows ("Genau ein Zitat"); the number lives
#: here so the validator and that line cannot mean different things.
_MAX_RELEASE_QUOTES = 1

#: A Q&A below this is a talking point with a question mark. Three is the floor at
#: which grouping the questions means anything at all.
_MIN_QA_QUESTIONS = 3

#: The profile field the release's dateline names. Read by the validator as well
#: as declared as a requirement, so it is written once: a rename that reached the
#: requirement but not the check would leave the dateline unheld and say nothing.
_SEAT_KEY = "sitz"

#: How much of the complaint travels into the retry prompt. A release can miss
#: six things at once, and a retry that ends in a paragraph of grievances is a
#: prompt the model reads past the part that mattered. The faults are appended in
#: contract order, so cutting the tail cuts the least structural half. The
#: refusal the consultant reads is never cut.
_MAX_FAULT_CHARS = 600

#: One retry, then refuse: two attempts in total, the same budget
#: :mod:`newspulse.analyzer` gives a batch. A model that missed the structure twice
#: is not going to find it on a third pass, and each attempt is a paid call a
#: person is waiting through.
_MAX_ATTEMPTS = 2

#: The markers the prompts ask for and the validators look for. Constants rather
#: than literals in two files, because the halves have to say exactly the same
#: thing: a validator hunting for a word the prompt never asked for rejects every
#: draft, twice, and then refuses in a way that looks like the model's fault.
_BRIDGE = "Brücke:"
_DO_NOT_SAY = "Nicht sagen"
_TOUCHY = "[heikel]"
_BOILERPLATE = "Über"
_GROUP = "##"

#: The sections an interview briefing owes, in the order it owes them. Named once:
#: they are the structure the definition declares, the headings the prompt asks
#: for, and what the validator counts.
_BRIEFING_SECTIONS = (
    "Wer fragt",
    "Was zuletzt erschien",
    "Womit zu rechnen ist",
    "Was gesagt werden soll",
    "Wo es unangenehm wird",
)

#: What a reply nobody could read is called, to the model on the retry and to the
#: consultant in the refusal. :class:`newspulse.analyzer.ParseError` says its piece
#: in English and quotes a JSON character offset, which is the right thing in a log
#: line and the wrong thing in the one sentence a person gets back from a button
#: they pressed, and the wrong thing again inside a German instruction block.
_UNREADABLE = "Die Antwort des Modells war nicht lesbar."

#: The one objection this module raises itself. Worded against what the reader will
#: actually see: the dash was in the reply, prose.plain() has already replaced it,
#: and what is left is a sentence worth re-reading rather than a dash to hunt for.
_DASH_CONCERN = (
    "Gedankenstrich im Entwurf, hier durch ein Komma ersetzt. Den Satz gegenlesen."
)

#: What the checker is told when a text was written with no profile on file. Not
#: "nothing to check": a text written off an empty profile is a text whose every
#: statement about the mandate is unbacked, which is exactly what to look for.
_NO_FACTS = (
    "Keine hinterlegt. Jede Tatsachenbehauptung über den Mandanten in diesem "
    "Text ist damit unbelegt."
)


@lru_cache(maxsize=None)
def _template(resource: str) -> Template:
    """One prompt file, read once per process.

    Writing all six formats off one impulse is six prompt files plus two checker
    prompts per text, and these are packaged resources: they cannot change under
    a running process, so re-reading them is a syscall per model call for a
    string that is already known.
    """
    # Composed, not just read: a prompt names the standards it is written under
    # in a `#blocks:` line, and brain.compose expands them (with any override
    # the agency has stored). Reading the file raw would send that line to the
    # model as text.
    return Template(brain.compose(resources.files("newspulse").joinpath(resource).read_text("utf-8")))


class Source(StrEnum):
    """Where a required input is read from.

    Four, because a mandate's facts live in four places that mean different
    things: the deep-dive profile a human filled, the mandate record itself, the
    impulse this text argues, and, for the one format that is written *at*
    somebody, the recipient :mod:`newspulse.pitch` can name from the coverage.
    """

    PROFIL = "profil"
    MANDAT = "mandat"
    IMPULS = "impuls"
    EMPFAENGER = "empfaenger"


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
_RECIPIENT_LABELS = {"outlet": "Ein belegtes Medium für das Gespräch"}

_WHERE = {
    Source.PROFIL: "im Profil",
    Source.MANDAT: "beim Mandanten",
    Source.IMPULS: "am Impuls",
    Source.EMPFAENGER: "in der Berichterstattung zum Themenfeld",
}

_LABELS_BY_SOURCE = {
    Source.MANDAT: _MANDATE_LABELS,
    Source.IMPULS: _ANGLE_LABELS,
    Source.EMPFAENGER: _RECIPIENT_LABELS,
}


def _label_for(source: Source, key: str) -> str | None:
    """The page's name for one required field, or ``None`` when nothing names it."""
    if source is Source.PROFIL:
        field = profile.FIELDS_BY_KEY.get(key)
        return field.label if field else None
    return _LABELS_BY_SOURCE[source].get(key)


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
    def field_name(self) -> str:
        """What the page calls this field. The raw key only if nothing names it,
        which :func:`_validate_registry` rules out for the formats that ship."""
        return _label_for(self.source, self.key) or self.key

    @property
    def label(self) -> str:
        """The field, and how much of it is needed.

        The count belongs in the label because a refusal has to name work the
        consultant can actually do. A guest article wants two stories under the
        impulse; told only "Belegte Meldungen" while looking at an impulse that
        visibly has one, he reads a bug rather than an instruction.
        """
        if self.minimum > 1:
            return f"{self.field_name}, mindestens {self.minimum}"
        return self.field_name

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


# --- The structural contract ---------------------------------------------------
#
# What each format owes, checked on the model's answer before it is stored. The
# prompt states the same contract and mostly gets it right; "mostly" is the reason
# this exists. A press release whose quote is attributed to nobody, a set of
# talking points with nine points and no bridges, a guest article that opens with
# a dateline: each of those is a text a consultant has to read to the end before
# discovering it is unusable, and each is mechanically detectable in a millisecond.
#
# What these check and what they deliberately do not: everything here is a shape,
# never a judgement. Whether the lead is any good, whether the quote says anything
# the body did not, whether the argument develops, that is what the crosscheck is
# for and it runs on a second model for a reason. A validator that starts weighing
# quality would refuse texts on taste, twice, and then refuse them for good.


@dataclass(frozen=True, slots=True)
class Given:
    """What the writer was handed, as a validator needs to see it.

    A validator may only hold the text against what the writing prompt actually
    carried. That is not a style rule: the spokesperson's name comes from the
    profile field the format named, so "is this quote attributed to the person the
    profile names" is answerable, and "is this quote real" is not.
    """

    #: The profile's spokesperson, for the formats that quote one.
    speaker: str = ""
    #: The sentences of the mandate's guide that name something it never says.
    #: A Q&A that avoids them is decoration, so they are checkable input.
    nogos: tuple[str, ...] = ()
    #: Who is asking, for the one format written at somebody.
    recipient: PitchTarget | None = None
    #: The mandate's seat, for the one format whose dateline has to name it. The
    #: field is a hard requirement of that format precisely so the dateline has a
    #: source, and without it here the validator could only check that *some*
    #: place was named, which is the invention the requirement was added against.
    seat: str = ""
    #: The day the text is being written, for the same dateline. Supplied to the
    #: prompt as a slot; carried here so the two halves cannot disagree.
    day: dt.date | None = None
    #: The headlines the writing prompt listed as this journalist's. A briefing
    #: may name these and no others, so the validator needs the same list rather
    #: than the target's own evidence tuple, which is a different one.
    headlines: tuple[str, ...] = ()


#: A format's structural check: the finished draft in, everything it fails to
#: carry out, as sentences a consultant can read.
Validator = Callable[[AssetDraft, Given], list[str]]


#: The months as a dateline prints them. Written out rather than left to
#: ``strftime("%B")``, which answers in whatever locale the process happens to
#: have been started under and would date a German release "20. August 2026" on
#: one machine and "20. August 2026" nowhere on the next.
_DE_MONTHS = (
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
)


#: A German dateline: place, comma, date. "Berlin, 20. August 2026" and
#: "Berlin, 20.08.2026" both count, because both are what a desk sees.
_DATELINE = re.compile(
    r"^\s*[^\n,]{2,40},\s*(?:\d{1,2}\.\s*[A-Za-zÄÖÜäöü]+|\d{1,2}\.\d{1,2}\.)\s*\d{4}"
)

#: A quotation of at least a clause. The length floor is what keeps a quoted
#: product name or a scare-quoted adjective from passing as the release's quote.
_QUOTE = re.compile(r"[\"„»“]([^\"„»“”«]{20,}?)[\"“«”]", re.S)

#: How a German release attributes a quote: the inquit. Either a speech verb
#: beside the quotation, or the "…, so Prot" form, which carries no verb at all
#: and is why the comma is part of that alternative rather than a bare "so".
#:
#: This is what tells the release's one quote apart from a scare-quoted phrase.
#: Without it, any quoted run of twenty characters counted as a quote, so a
#: release carrying its quote and one quoted phrase was refused for "2 Zitate",
#: retried, refused again, and delivered nothing.
_INQUIT = re.compile(
    r"\b(?:sagt|sagte|erklärt|erklärte|betont|betonte|ergänzt|ergänzte|"
    r"kommentiert|kommentierte|berichtet|berichtete|meint|meinte|"
    r"fügt|führt|unterstreicht|resümiert)\b"
    r"|,\s*so\s+[A-ZÄÖÜ]"
)

#: How long the inquit between the two halves of a split quote may be.
#: "…“, sagt Alexandra Prot, Geschäftsführerin der Alpha AG, „…" is the long end
#: of what a desk prints; past this the two halves are two quotes with a sentence
#: standing between them.
_MAX_INQUIT_CHARS = 100

#: The release's boilerplate, as a heading rather than as a prefix. Bare
#: ``startswith("Über")`` also matched a paragraph opening "Überdies" or
#: "Überraschend", which is a body paragraph being read as the boilerplate.
_BOILERPLATE_START = re.compile(rf"{re.escape(_BOILERPLATE)}\b")

#: The "Nicht sagen" block's heading, as a heading: at the start of its own line,
#: with or without markup. ``partition`` on the first occurrence anywhere cut the
#: points in half whenever one of them contained the words, and then reported the
#: bridges under the lost half as missing.
_DO_NOT_SAY_HEADING = re.compile(
    rf"^[#\s*]*{re.escape(_DO_NOT_SAY)}\b.*$", re.IGNORECASE | re.MULTILINE
)

#: Tokens that end in a full stop without ending a sentence. Splitting on every
#: ". " counted "Das gilt z. B. für Banken." as three sentences, which pushed an
#: ordinary four-sentence statement past the five-sentence bound and refused it
#: twice. It also cut "sagt Dr. Alexandra Prot" in half, which is where the
#: release's attribution is read. Short on purpose: a name missing from this set
#: costs a wrong sentence count, a wrong entry runs two sentences together.
_ABBREVIATIONS = frozenset(
    {
        "abs",
        "bspw",
        "bzw",
        "ca",
        "dipl",
        "dr",
        "etc",
        "evtl",
        "ggf",
        "hrsg",
        "inkl",
        "ing",
        "max",
        "min",
        "mio",
        "mrd",
        "nr",
        "prof",
        "sog",
        "usw",
        "vgl",
    }
)

_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")
_TRAILING_TOKEN = re.compile(r"([\wÄÖÜäöüß]+)\.$")

#: The date inside a dateline, either spelling. Read rather than pattern-matched,
#: because "20. August 2026" and "20.08.2026" are the same day and the question
#: the release is held to is whether it is *today*.
_DATELINE_DATE = re.compile(
    r"(\d{1,2})\.\s*(?:([A-Za-zÄÖÜäöü]+)|(\d{1,2})\.)\s*(\d{4})"
)

#: A numbered talking point. The number is the format: a consultant scanning this
#: in a green room counts them, and an unnumbered list is prose.
_POINT = re.compile(r"^\s*\d+[.)]\s+\S")

#: First person, singular or plural. A guest article without one of these is an
#: institutional text with somebody's name under it, which is the failure mode
#: every agency guest article has.
#:
#: The possessives are spelled out to their real inflections rather than left as
#: ``mein\w*``: that matched "Meinung", "meinte" and "meinen", so "Nach Meinung
#: vieler Beobachter …" satisfied the first-person check with no first person in
#: it, which is precisely the text this is here to catch.
_FIRST_PERSON = re.compile(
    r"\b(ich|mir|mich|mein(e|es|em|en|er)?|wir|uns|unser(e|es|em|en|er)?)\b",
    re.IGNORECASE,
)

#: Openings that make a text a news story rather than an argument. Checked only
#: against a guest article's first sentence: "am Montag" in the middle of a
#: paragraph is a date, at the top it is a news hook the op-ed page did not ask for.
#:
#: Every entry names a specific moment. The bare "vergangenen" used to be here and
#: rejected "In den vergangenen Jahren habe ich eine Verschiebung beobachtet",
#: which is an ordinary op-ed opening; a miss here costs two paid calls and
#: delivers no text, so the entries have to be as narrow as what they catch.
_NEWS_LEAD_OPENERS = (
    "vergangene woche",
    "vergangenen woche",
    "am vergangenen",
    "in dieser woche",
    "diese woche",
    "am montag",
    "am dienstag",
    "am mittwoch",
    "am donnerstag",
    "am freitag",
    "wie bekannt wurde",
    "wie diese woche bekannt",
    "hat heute",
    "gab heute bekannt",
    "gab bekannt",
    "kürzlich hat",
)

#: What a statement must not open with. A statement begins with the statement;
#: everything here is the sound of a text clearing its throat, and a desk cuts it
#: along with the first sentence that followed it.
_PREAMBLE_OPENERS = (
    "sehr geehrte",
    "liebe ",
    "guten tag",
    "wir haben mit interesse",
    "mit interesse haben wir",
    "zunächst einmal",
    "vorab",
)

#: How long a word out of a No-Go has to be before it counts as naming that No-Go's
#: subject. Below this it is grammar, and matching on grammar would clear every
#: text ever written.
_MIN_NOGO_TERM = 6

#: Long enough to pass the floor above and still say nothing about a subject.
#: Their only effect is to make the No-Go check pass, so the list is kept short and
#: the bias is deliberate: a wrongly refused Q&A costs a text, a wrongly accepted
#: one costs a second read.
_NOGO_NOISE = frozenset(
    {
        "werden",
        "wurden",
        "sollte",
        "sollten",
        "sollen",
        "müssen",
        "niemals",
        "immer",
        "andere",
        "anderen",
        "welche",
        "unsere",
        "unserem",
        "unseren",
        "keinen",
        "keine",
        "nicht",
    }
)

#: A labelled section in a guide: "Positionierung:", "No-Gos:", "Tonalität:".
#: Up to three words and a colon, which is what :mod:`newspulse.guide` asks the
#: distiller for and what a consultant types by hand. It is the boundary of the
#: No-Go block: everything from the marker to the next label is a No-Go, and a
#: sentence like "Nie über Preise sprechen." carries no colon and does not end it.
_GUIDE_LABEL = re.compile(r"^[·\s]*\w[\w&/-]*(?:\s+[\w&/-]+){0,2}\s*:")

#: How many terms out of the guide's No-Gos the Q&A check considers. A guide runs
#: to a few hundred words; past a dozen terms the check stops being "does this ask
#: the uncomfortable question" and starts matching on incidental vocabulary.
_MAX_NOGO_TERMS = 12

#: How much of a No-Go's word has to appear before the Q&A counts as having asked
#: about it. The same floor the terms are collected at, because a stem shorter
#: than the shortest term worth collecting would match on grammar again.
_NOGO_STEM = _MIN_NOGO_TERM

#: What a consultant calls the things the mandate never says. "No-Go" is what
#: :func:`newspulse.guide.distill` writes and what most guides use, and for a
#: while it was the only one recognised: a guide headed "Tabus:" produced no
#: No-Gos at all, so the Q&A's central check silently did not run and nothing
#: anywhere said so. Over-collection is the safe direction here, since extra
#: terms can only make that check pass.
_NOGO_MARKERS = (
    "no-go",
    "no go",
    "nogo",
    "tabu",
    "nie sagen",
    "niemals sagen",
    "sagen wir nie",
    "was wir nicht sagen",
)


def _paragraphs(text: str) -> list[str]:
    return [block.strip() for block in re.split(r"\n\s*\n", text or "") if block.strip()]


def _lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _abbreviated(part: str) -> bool:
    """Whether this piece ends in an abbreviation rather than in a sentence."""
    match = _TRAILING_TOKEN.search(part)
    if not match:
        return False
    token = match.group(1).casefold()
    # A single letter is "z. B.", "u. a.", an initial: never a sentence on its own.
    return len(token) == 1 or token in _ABBREVIATIONS


def _sentences(text: str) -> list[str]:
    """The text's sentences, counted the way a subeditor counts them.

    "z. B." and "Dr." end no sentence. That matters in two places that both cost
    a text when they are wrong: the statement's three-to-five bound is counted off
    this, and the release's attribution is read out of the clause around its
    quote, which "sagt Dr. Alexandra Prot" would otherwise be cut out of.
    """
    merged: list[str] = []
    for piece in _SENTENCE_BREAK.split((text or "").strip()):
        if not piece:
            continue
        if merged and _abbreviated(merged[-1]):
            merged[-1] = f"{merged[-1]} {piece}"
        else:
            merged.append(piece)
    return merged


def _clause_before(text: str) -> str:
    """The clause a quotation follows, no further back than its own paragraph."""
    parts = _sentences(re.split(r"\n\s*\n", text)[-1])
    return parts[-1] if parts else ""


def _clause_after(text: str) -> str:
    """The clause a quotation runs into, no further on than its own paragraph."""
    parts = _sentences(re.split(r"\n\s*\n", text)[0])
    return parts[0] if parts else ""


def _surname(name: str) -> str:
    """The last word of the personal name, which is what a text refers back to.

    A profile holds "Alexandra Prot, Geschäftsführerin"; a body says "sagt Prot".
    Matching on the whole stored value would fail on the second mention of every
    correctly attributed quote there is.
    """
    personal = (name or "").split(",")[0].strip()
    return personal.split()[-1] if personal.split() else ""


def _names(text: str, person: str) -> bool:
    """Whether ``text`` refers to this person at all.

    On a word boundary, never as a bare substring. This is the check the release's
    attribution rests on, and German surnames are ordinary words or live inside
    them: Lang in "langfristig", Klein in "kleinen", Reich in "Bereich", Berg in
    "Bergbau". A substring match clears a quote attributed to an invented CFO as
    long as the paragraph happens to use one of those, which is exactly the
    artefact this module exists to prevent.

    The genitive ``s`` is allowed because "Langs Einschätzung" names the same
    person, and the cost of a wrong refusal here is two paid calls and no text.
    Lookarounds rather than ``\\b`` so a stored value that ends in punctuation
    still anchors: ``\\b`` before a non-word character never matches at all.
    """
    surname = _surname(person)
    if not surname:
        return False
    pattern = rf"(?<!\w){re.escape(surname)}(?:s|'s|’s)?(?!\w)"
    return bool(re.search(pattern, text or "", re.IGNORECASE))


def nogo_terms(nogos: tuple[str, ...]) -> list[str]:
    """The words out of a mandate's No-Gos that name what they are about."""
    terms: list[str] = []
    for line in nogos:
        for word in re.findall(r"[\wÄÖÜäöüß]+", line):
            folded = word.casefold()
            if len(folded) >= _MIN_NOGO_TERM and folded not in _NOGO_NOISE:
                terms.append(folded)
    return list(dict.fromkeys(terms))[:_MAX_NOGO_TERMS]


def _mentions(text: str, terms: list[str]) -> bool:
    """Whether ``text`` names any of these No-Go subjects, inflections included.

    On the stem, not on the whole word. German inflects and compounds: a guide
    saying "Keine Vergleiche mit Wettbewerbern" is answered by a question about
    "dem Wettbewerber X", and an exact-substring match refuses that Q&A, retries,
    refuses again and delivers no text. Matching short can only make the check
    pass, and a Q&A wrongly cleared costs a second read where one wrongly refused
    costs the whole thing.
    """
    folded = text.casefold()
    return any(term[:_NOGO_STEM] in folded for term in terms)


def _release_contract(draft: AssetDraft, given: Given) -> list[str]:
    """Headline, dateline, lead, body, one attributed quote, boilerplate.

    The quote's attribution is checked inside the paragraph that carries the
    quote rather than anywhere in the text. The mandate's spokesperson is named in
    the boilerplate of half these releases, so "the name appears somewhere" would
    clear a quote attributed to an invented CFO two paragraphs above it. That is
    the artefact this whole feature is built to prevent.

    And checked in *every* such paragraph, not in one of them. A release carrying
    three quotes, one correctly attributed and two put in the mouths of invented
    executives, passes an "at least one of them names the speaker" test with
    nothing to object to, and the two inventions are what gets printed.
    """
    faults: list[str] = []
    paragraphs = _paragraphs(draft.body)
    if not draft.title.strip():
        faults.append("Die Schlagzeile fehlt.")
    faults.extend(_dateline_faults(paragraphs[0] if paragraphs else "", given))
    if len(paragraphs) < _MIN_RELEASE_PARAGRAPHS:
        faults.append(
            f"Der Text hat {len(paragraphs)} Absätze. Lead, Fließtext und "
            f"Boilerplate sind mindestens {_MIN_RELEASE_PARAGRAPHS}."
        )
    faults.extend(_quote_faults(draft.body, given.speaker))
    if not paragraphs or not _BOILERPLATE_START.match(paragraphs[-1]):
        faults.append(f'Die Boilerplate fehlt: kein Absatz beginnt mit "{_BOILERPLATE}".')
    return faults


def _city(seat: str) -> str:
    """The place a dateline would print out of a stored seat.

    A profile holds "Berlin", "Berlin, Deutschland" or "Berlin (Mitte)"; a
    dateline prints the city. Comparing the whole stored value would refuse the
    correct dateline for every mandate whose seat was typed with a country.
    """
    return (seat or "").split(",")[0].split("(")[0].strip()


def _dateline_day(text: str) -> dt.date | None:
    """The day a dateline is stamped with, in either spelling, or ``None``."""
    match = _DATELINE_DATE.search(text)
    if match is None:
        return None
    day, month_name, month_number, year = match.groups()
    if month_name:
        names = [name.casefold() for name in _DE_MONTHS]
        if month_name.casefold() not in names:
            return None
        month = names.index(month_name.casefold()) + 1
    else:
        month = int(month_number)
    try:
        return dt.date(int(year), month, int(day))
    except ValueError:
        return None


def _dateline_faults(opening: str, given: Given) -> list[str]:
    """Whether the dateline says the place and the date the writer was handed.

    Both were supplied for exactly this. The seat is a hard requirement of the
    format, today's date travels into the prompt as a slot, and the prompt says
    of both that they are copied and not replaced. A release datelined from a
    city the mandate does not sit in, or from a date that is not today, is an
    invented fact of the kind the refusal block forbids two paragraphs above, and
    unlike a clumsy sentence it is a send failure the desk sees before the news.

    Checked only against what was actually handed over: a ``Given`` carrying
    neither, which is what a caller validating a text by hand passes, gets the
    structural check and no judgement about facts it was not told.
    """
    found = _DATELINE.search(opening)
    if found is None:
        return ["Die Dateline (Ort, Datum) fehlt am Anfang."]
    line = found.group(0)
    place = line.partition(",")[0]
    faults: list[str] = []
    seat = _city(given.seat)
    if seat and seat.casefold() not in place.casefold():
        faults.append(
            f"Die Dateline nennt {place.strip()}, nicht den Sitz des Mandanten "
            f"({seat})."
        )
    if given.day is not None and _dateline_day(line) != given.day:
        faults.append(
            f"Die Dateline ist nicht auf heute datiert. Heute ist der "
            f"{today(day=given.day)}."
        )
    return faults


@dataclass(frozen=True, slots=True)
class _Quotation:
    """One quotation in a release, with the text that could attribute it.

    Three pieces rather than one string, because the two questions asked of it
    are different: whether anything presents this as somebody's words at all, and
    whose words it says they are.
    """

    #: The clause the quotation follows.
    before: str
    #: What stands between the halves of a split quote, which is the inquit.
    inner: str
    #: The clause the quotation runs into.
    after: str

    @property
    def frame(self) -> str:
        parts = (self.before, self.inner, self.after)
        return " ".join(part.strip() for part in parts if part.strip())

    @property
    def spoken(self) -> bool:
        """Whether anything around it presents it as somebody's words."""
        return bool(_INQUIT.search(self.frame)) or self.before.rstrip().endswith(":")


def _is_inquit_gap(gap: str) -> bool:
    """Whether what stands between two quotations is an inquit and nothing else."""
    clause = gap.strip()
    return (
        clause.startswith(",")
        and clause.endswith(",")
        and len(clause) <= _MAX_INQUIT_CHARS
        and bool(_INQUIT.search(clause))
    )


def _quotations(body: str) -> list[_Quotation]:
    """The body's quotations, one per quotation rather than one per quote mark.

    Two things this gets right that counting ``_QUOTE`` matches does not.

    The split quote is one quote. "„Verfügbarkeit ist ein Risikoparameter“, sagt
    Prot, „und die Banken tragen es selbst.“" is the most ordinary construction in
    a German release and the most ordinary thing for a model to write. Counted as
    two, a correct release was refused for carrying two quotes, retried with a
    complaint it could only answer by writing unnatural German, and then refused
    for good. The halves are joined again when what stands between them is an
    inquit and nothing else.

    And the attribution is read out of the clause, not the paragraph. "…, sagt
    Finanzvorstand Thomas Lang. Alexandra Prot war nicht erreichbar." names the
    profile's spokesperson in the same paragraph and puts the quote in an invented
    CFO's mouth, which is the artefact this module exists to prevent.
    """
    runs: list[list[tuple[int, int]]] = []
    for match in _QUOTE.finditer(body):
        span = match.span()
        if runs and _is_inquit_gap(body[runs[-1][-1][1] : span[0]]):
            runs[-1].append(span)
        else:
            runs.append([span])
    return [
        _Quotation(
            before=_clause_before(body[: run[0][0]]),
            inner=" ".join(body[left[1] : right[0]] for left, right in zip(run, run[1:])),
            after=_clause_after(body[run[-1][1] :]),
        )
        for run in runs
    ]


def _quote_faults(body: str, speaker: str) -> list[str]:
    """What is wrong with the release's quoting: how many, and whose.

    Counted as quotations rather than as quote marks. A scare-quoted phrase, a
    quoted product name and the second half of a split quote are not the
    release's second quote, and refusing a release for two quotes it does not
    carry costs a second paid call and then the text.

    ``_MAX_RELEASE_QUOTES`` is the contract the definition and the prompt both
    state, and it is enforced here rather than taken on trust, because the number
    is what makes the attribution check exhaustive: with one quotation there is
    one mouth to check, and the retry carrying this complaint tells the model
    exactly which sentence to drop.
    """
    spoken = [quote for quote in _quotations(body) if quote.spoken]
    if not spoken:
        return ["Es steht kein Zitat in der Meldung."]
    faults: list[str] = []
    if len(spoken) > _MAX_RELEASE_QUOTES:
        faults.append(
            f"Die Meldung enthält {len(spoken)} Zitate. Verlangt ist genau "
            f"{_MAX_RELEASE_QUOTES}, damit erkennbar bleibt, wer spricht."
        )
    if any(not _names(quote.frame, speaker) for quote in spoken):
        faults.append(
            f"Das Zitat ist nicht {speaker or 'der im Profil genannten Person'} "
            "zugeschrieben."
        )
    return faults


def _statement_contract(draft: AssetDraft, given: Given) -> list[str]:
    """Three to five sentences, the attribution under them, nothing in front."""
    faults: list[str] = []
    lines = _lines(draft.body)
    attribution = lines[-1] if lines else ""
    spoken = " ".join(lines[:-1]) if len(lines) > 1 else ""
    if not _names(attribution, given.speaker):
        faults.append(
            f"Unter dem Statement steht keine Zuschreibung an "
            f"{given.speaker or 'die im Profil genannte Person'}."
        )
        # Without an attribution line the whole body is the statement, and
        # counting its sentences against the last line would report a number the
        # reader cannot find in the text.
        spoken = draft.body
    count = len(_sentences(spoken))
    if not _MIN_STATEMENT_SENTENCES <= count <= _MAX_STATEMENT_SENTENCES:
        faults.append(
            f"Das Statement hat {count} Sätze, verlangt sind "
            f"{_MIN_STATEMENT_SENTENCES} bis {_MAX_STATEMENT_SENTENCES}."
        )
    if spoken.strip().casefold().startswith(_PREAMBLE_OPENERS):
        faults.append("Das Statement beginnt mit einem Vorlauf statt mit der Aussage.")
    return faults


def _qa_contract(draft: AssetDraft, given: Given) -> list[str]:
    """Questions, grouped, the uncomfortable ones marked and actually asked.

    Two things are counted over the questions rather than over the body, and both
    are the format's whole point. A group heading shaped like a question
    ("## Und die Kosten?") is a heading, so three of those satisfied the floor
    over a Q&A carrying one real pair. And a No-Go named only in an *answer* is
    the reassuring text the prompt forbids: the uncomfortable question has to be
    on the page, which is the one thing a consultant cannot write for himself in
    the ten minutes before the call.
    """
    faults: list[str] = []
    lines = _lines(draft.body)
    questions = [
        line for line in lines if line.endswith("?") and not line.startswith(_GROUP)
    ]
    if len(questions) < _MIN_QA_QUESTIONS:
        faults.append(
            f"Das Q&A stellt {len(questions)} Fragen, mindestens "
            f"{_MIN_QA_QUESTIONS} sind verlangt."
        )
    if not any(line.startswith(_GROUP) for line in lines):
        faults.append(
            f'Die Fragen sind nicht gruppiert: keine Überschrift beginnt mit "{_GROUP}".'
        )
    if _TOUCHY not in draft.body:
        faults.append(f'Keine Frage ist als unangenehm markiert ("{_TOUCHY}").')
    terms = nogo_terms(given.nogos)
    if terms and not _mentions(" ".join(questions), terms):
        faults.append(
            "Keine Frage rührt an die No-Gos des Guides. Genau die werden gestellt."
        )
    return faults


def _split_do_not_say(body: str) -> tuple[str, str | None]:
    """The points and what must not be said, split at the heading between them.

    At the heading, and at the first one that is a heading. Splitting on the first
    occurrence of the words anywhere put a point reading "Nicht sagen, dass die
    Kette unzuverlässig sei" in the wrong half, took every bridge below it with
    it, and then reported those bridges as missing.
    """
    match = _DO_NOT_SAY_HEADING.search(body or "")
    if match is None:
        return body or "", None
    return body[: match.start()], body[match.end() :]


def _bridgeless(head: str) -> list[str]:
    """The numbered points that carry no bridge before the next point starts.

    Counted as pairs rather than as two totals. Three bridges stacked under point
    one with points two and three bare satisfies "as many bridges as points" and
    is exactly the artefact the format exists to prevent: a consultant in a green
    room with two points and no way back to the thesis.
    """
    bare: list[str] = []
    current: str | None = None
    bridged = False
    for line in _lines(head):
        if _POINT.match(line):
            if current is not None and not bridged:
                bare.append(current)
            current, bridged = line, False
        elif current is not None and line.startswith(_BRIDGE):
            bridged = True
    if current is not None and not bridged:
        bare.append(current)
    return bare


def _talking_points_contract(draft: AssetDraft, given: Given) -> list[str]:
    """A bounded number of points, a bridge under each, and what not to say.

    The points are counted before the "Nicht sagen" heading only. What must not be
    said is often a numbered list too, and counting those against the cap would
    refuse a perfectly good set of four points for having three things to avoid.
    """
    faults: list[str] = []
    head, tail = _split_do_not_say(draft.body)
    points = [line for line in _lines(head) if _POINT.match(line)]
    bare = _bridgeless(head)
    if not points:
        faults.append("Der Text enthält keine nummerierten Punkte.")
    elif len(points) > MAX_TALKING_POINTS:
        faults.append(
            f"{len(points)} Punkte. Talking Points sind höchstens "
            f"{MAX_TALKING_POINTS}, sonst liest sie im Gespräch niemand mehr."
        )
    if bare:
        faults.append(
            f"Ohne Brücke zurück zur These: {len(bare)} von {len(points)} Punkten. "
            f'Unter jedem Punkt braucht es eine Zeile "{_BRIDGE} …".'
        )
    if tail is None or not tail.strip():
        faults.append(f'Der Abschnitt "{_DO_NOT_SAY}" fehlt.')
    return faults


def _gastbeitrag_contract(draft: AssetDraft, given: Given) -> list[str]:
    """Argued, first person, roughly an op-ed page, and not a release in disguise.

    The dateline is looked for in every paragraph and the news hook in the whole
    lead paragraph, not in the first sentence of the first one. "It cannot be
    mistaken for a release" is the acceptance, and a dateline in paragraph two is
    read by a desk as a release with a preamble; the hook, likewise, lands in the
    lead paragraph rather than always in its opening words.
    """
    faults: list[str] = []
    paragraphs = _paragraphs(draft.body)
    lead = paragraphs[0] if paragraphs else ""
    if any(_DATELINE.search(block) for block in paragraphs):
        faults.append("Der Beitrag trägt eine Dateline. Er ist dann eine Mitteilung.")
    hook = next((o for o in _NEWS_LEAD_OPENERS if o in lead.casefold()), "")
    if hook:
        faults.append(
            f'Der Beitrag beginnt mit einem Nachrichtenaufhänger ("{hook}") '
            "statt mit der These."
        )
    if not _FIRST_PERSON.search(draft.body):
        faults.append("Der Beitrag steht nicht in der ersten Person.")
    length = len(draft.body)
    if not _MIN_GASTBEITRAG_CHARS <= length <= _MAX_GASTBEITRAG_CHARS:
        faults.append(
            f"{length} Zeichen. Verlangt sind rund {GASTBEITRAG_CHARS}, "
            f"vertretbar {_MIN_GASTBEITRAG_CHARS} bis {_MAX_GASTBEITRAG_CHARS}."
        )
    return faults


def _briefing_contract(draft: AssetDraft, given: Given) -> list[str]:
    """Who is asking, what they wrote, what they will ask, what to say anyway.

    The headlines are held to the list the prompt carried, verbatim. "Names the
    journalist's recent headlines" is half of what this format is for, and the
    failure it has is not omission but invention: a briefing that credits a piece
    the journalist did not write is read out loud in the first minute of the
    interview. When no headline was supplied the prompt said so and asked for a
    sentence to that effect, and there is nothing here to hold it to.
    """
    faults: list[str] = []
    body = draft.body.casefold()
    target = given.recipient
    outlet = (target.outlet if target else "") or ""
    if outlet and outlet.casefold() not in body:
        faults.append(f"Das Medium ({outlet}) kommt im Briefing nicht vor.")
    journalist = (target.journalist if target else "") or ""
    if journalist and not _names(draft.body, journalist):
        faults.append(f"Der Fragesteller ({journalist}) kommt im Briefing nicht vor.")
    supplied = [line.strip() for line in given.headlines if line.strip()]
    if supplied and not any(headline in draft.body for headline in supplied):
        faults.append(
            "Keine der belegten Schlagzeilen steht im Briefing. Unter "
            f'"{_BRIEFING_SECTIONS[1]}" gehören genau diese und keine anderen.'
        )
    absent = [
        section for section in _BRIEFING_SECTIONS if section.casefold() not in body
    ]
    if absent:
        faults.append(f"Es fehlen die Abschnitte: {', '.join(absent)}.")
    return faults


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
    #: The machine-checkable half of ``structure``, run on the model's answer
    #: before anything is stored. ``None`` means the contract is stated in the
    #: prompt and taken on trust, which is what a seventh format starts as.
    validator: Validator | None = None

    @property
    def needs_recipient(self) -> bool:
        """Whether this format is written at somebody. Only the briefing is."""
        return any(req.source is Source.EMPFAENGER for req in self.requires)

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
        return _template(self.prompt)


FORMATS: tuple[FormatDef, ...] = (
    FormatDef(
        kind=AssetKind.PRESSEMITTEILUNG,
        name="Pressemitteilung",
        description="Die offizielle Meldung des Mandanten, zitierfähig und datiert.",
        prompt="prompts/pressemitteilung.txt",
        # The seat is a requirement because the dateline is: the validator demands
        # a place and a date at the top of the first paragraph, the date arrives as
        # a slot, and the only honest source for the city is the profile field that
        # holds it. Without it the prompt asks for a dateline it supplies nothing
        # for, while forbidding invention two blocks above, and the release is
        # refused twice into a Malformed instead of once with a field to fill.
        requires=(
            Requirement(Source.PROFIL, "ceo"),
            Requirement(Source.PROFIL, "geschaeftsfeld"),
            Requirement(Source.PROFIL, "sitz"),
        ),
        structure=(
            "Eine Schlagzeile, die die Nachricht enthält und nicht bewirbt.",
            "Eine Dateline als Anfang des ersten Absatzes: Ort, Datum.",
            "Ein Lead, der in einem Satz sagt, was passiert ist.",
            "Ein Fließtext, der ihn belegt.",
            "Genau ein Zitat in Anführungszeichen, im selben Satz zugeschrieben "
            'an die oben genannte Person ("…", sagt <Name>, <Funktion>.).',
            f'Eine Boilerplate als letzter Absatz, beginnend mit "{_BOILERPLATE} …".',
            f"Mindestens {_MIN_RELEASE_PARAGRAPHS} Absätze, getrennt durch Leerzeilen.",
        ),
        title_label="Schlagzeile",
        speaker_key="ceo",
        validator=_release_contract,
    ),
    FormatDef(
        kind=AssetKind.STATEMENT,
        name="Statement",
        description="Drei bis fünf zitierfähige Sätze einer namentlichen Person.",
        prompt="prompts/statement.txt",
        requires=(Requirement(Source.PROFIL, "ceo"),),
        structure=(
            f"{_MIN_STATEMENT_SENTENCES} bis {_MAX_STATEMENT_SENTENCES} Sätze, "
            "so zitierfähig, wie sie gedruckt würden.",
            "Die Zuschreibung als letzte Zeile, allein: Name und Funktion.",
            "Kein Vorlauf, keine Einordnung, keine Anrede.",
        ),
        speaker_key="ceo",
        validator=_statement_contract,
    ),
    FormatDef(
        kind=AssetKind.QA,
        name="Q&A",
        description="Die Fragen, die kommen, samt der unangenehmen.",
        prompt="prompts/qa.txt",
        requires=(Requirement(Source.MANDAT, "comms_guide"),),
        structure=(
            f'Fragen und Antworten, nach Themen gruppiert; jede Gruppe als "{_GROUP} '
            'Thema"-Überschrift.',
            f"Mindestens {_MIN_QA_QUESTIONS} Fragen, jede als eigene Zeile mit "
            "Fragezeichen.",
            f'Die unangenehmen Fragen mit vorangestelltem "{_TOUCHY}" markiert.',
            "Mindestens eine Frage zu einem No-Go aus dem Guide, in den Worten des "
            "Guides.",
            "Jede Antwort für sich sprechbar, ohne die vorherige.",
        ),
        validator=_qa_contract,
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
            f"Höchstens {MAX_TALKING_POINTS} Punkte, nummeriert.",
            f'Unter jedem Punkt eine Zeile "{_BRIDGE} …": die Brücke zurück zur These.',
            f'Ein eigener Abschnitt "{_DO_NOT_SAY}" am Ende.',
        ),
        validator=_talking_points_contract,
    ),
    FormatDef(
        kind=AssetKind.GASTBEITRAG,
        name="Gastbeitrag",
        description="Ein argumentierter Text in der ersten Person, ohne Nachrichtenaufhänger.",
        prompt="prompts/gastbeitrag.txt",
        # A guest article is printed under somebody's byline, so the author is a
        # required fact like the release's spokesperson is. Without this the prompt
        # asked for a name and the only place to find one was the free-text profile
        # block, which is exactly the inference DEC-2 exists to forbid.
        requires=(
            Requirement(Source.PROFIL, "ceo"),
            Requirement(Source.IMPULS, "thesis"),
            Requirement(
                Source.IMPULS, "article_ids", minimum=_MIN_GASTBEITRAG_EVIDENCE
            ),
        ),
        structure=(
            f"Rund {GASTBEITRAG_CHARS} Zeichen, nicht unter "
            f"{_MIN_GASTBEITRAG_CHARS} und nicht über {_MAX_GASTBEITRAG_CHARS}.",
            "Erste Person, ein Argument, das sich entwickelt.",
            "Kein Nachrichtenaufhänger, keine Dateline, kein Zitat von sich selbst.",
        ),
        speaker_key="ceo",
        validator=_gastbeitrag_contract,
    ),
    FormatDef(
        kind=AssetKind.INTERVIEW_BRIEFING,
        name="Interview-Briefing",
        description="Wer fragt, was er zuletzt schrieb, und was gesagt werden soll.",
        prompt="prompts/interview_briefing.txt",
        # The outlet is a requirement like the release's spokesperson is. A
        # briefing for an unnamed medium is a page of talking points with the
        # wrong title, and the one thing it must never do is invent the masthead.
        # What can name one honestly is newspulse.pitch, off the same coverage the
        # letter is aimed with.
        requires=(
            Requirement(Source.IMPULS, "article_ids"),
            Requirement(Source.EMPFAENGER, "outlet"),
        ),
        structure=tuple(
            f'Ein Abschnitt "{section}".' for section in _BRIEFING_SECTIONS
        )
        + (
            "Das Medium beim Namen, und die Person, wenn oben eine belegt ist.",
            "Keine erfundenen Beiträge: nur die oben belegten Schlagzeilen.",
        ),
        validator=_briefing_contract,
    ),
)

REGISTRY: dict[str, FormatDef] = {fmt.key: fmt for fmt in FORMATS}


def _validate_registry() -> None:
    """Fail at import when a definition names a field nothing can label.

    Same posture as :func:`definition` on an unknown kind, and for a worse
    failure: an unlabelled key falls back to the key itself, so a typo turns
    into a refusal that sends the consultant to fill "geschaftsfeld" on a page
    that has no such row. He cannot act on that and cannot tell it from a real
    gap. Loud at import is the one place it costs nothing.
    """
    for fmt in FORMATS:
        for req in fmt.requires:
            if _label_for(req.source, req.key) is None:
                raise RuntimeError(
                    f"{fmt.key}: requirement {req.source.value}/{req.key} has no "
                    "label; add one or fix the key"
                )
        if fmt.speaker_key and fmt.speaker_key not in profile.FIELDS_BY_KEY:
            raise RuntimeError(
                f"{fmt.key}: speaker_key {fmt.speaker_key!r} is not a profile field"
            )
    if _SEAT_KEY not in profile.FIELDS_BY_KEY:
        raise RuntimeError(
            f"the dateline is written from profile field {_SEAT_KEY!r}, which no "
            "longer exists; fix the key or drop the check"
        )


_validate_registry()


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


class Malformed(ParseError):
    """The model answered twice without carrying the format's structure.

    A :class:`newspulse.analyzer.ParseError` by inheritance, so a caller that
    already handles "the model did not answer usably" keeps working and the
    retry precedent stays one concept. What it adds is :attr:`reason`: a sentence
    in the consultant's language saying what the text was missing, because unlike
    a dropped analysis batch this refusal has somebody standing in front of it who
    pressed a button and is owed an answer better than nothing happening.
    """

    def __init__(self, fmt: FormatDef, fault: str) -> None:
        self.fmt = fmt
        self.fault = fault
        self.reason = (
            f"{fmt.name} nicht geschrieben. Auch der zweite Versuch trug die "
            f"verlangte Struktur nicht: {fault}"
        )
        super().__init__(self.reason)


def recipient(
    session: Session, client: Client, angle: Angle | None
) -> PitchTarget | None:
    """Who the one format that is written at somebody is written at.

    The best-evidenced target :mod:`newspulse.pitch` can name for this impulse:
    the byline on the stories the impulse rests on, then the field's regulars,
    then the outlets covering it. Deliberately the same list the letter is
    addressed with, so an interview briefing and a pitch cannot disagree about
    who covers this mandate's field.

    ``None`` when the coverage names nobody, which is a refusal for the briefing
    rather than an invitation to invent a masthead.
    """
    targets = pitch.targets_for(session, client, angle)
    return targets[0] if targets else None


class _Unresolved:
    """Nobody has looked the recipient up yet.

    A sentinel rather than ``None``, because the two mean opposite things and the
    difference is a database round trip on the path that is about to refuse.
    ``None`` is an answer: :func:`recipient` ran and the coverage named nobody. Read
    as "not looked up yet" it sends the readiness check back through
    :func:`newspulse.pitch.targets_for` — several queries and a contact lookup per
    candidate — to be told the same thing a second time.
    """


_UNRESOLVED = _Unresolved()

#: What a caller may hand in for the recipient: one, none, or nothing yet.
Recipient = PitchTarget | None | _Unresolved


def _resolved(
    session: Session,
    fmt: FormatDef,
    client: Client,
    angle: Angle | None,
    target: Recipient,
) -> PitchTarget | None:
    """The passed recipient, or the one the coverage names, for a format that needs one.

    Resolved lazily and once: :func:`write` looks it up before the requirement
    check and hands the same object to the readiness check, the prompt and the
    validator, so the briefing is written for exactly the recipient it was cleared
    for and the lookup happens one time.
    """
    if not isinstance(target, _Unresolved):
        return target
    return recipient(session, client, angle) if fmt.needs_recipient else None


def requirements_met(
    session: Session,
    fmt: FormatDef,
    client: Client,
    angle: Angle | None = None,
    *,
    facts: dict[str, ClientFact] | None = None,
    target: Recipient = _UNRESOLVED,
) -> Readiness:
    """What this format is still missing for this mandate.

    ``angle`` may be ``None``: the surface asks which formats a mandate could
    write before an impulse is picked, and everything the impulse would supply is
    then honestly reported as missing rather than assumed.

    ``facts`` is the mandate's profile when the caller already holds it. Writing
    one format reads the profile three times otherwise, and FMT-03 writes six in
    a row off one impulse. ``target`` is the same courtesy for the recipient: hand
    in one that has already been looked up, including the ``None`` that means the
    coverage named nobody, and no lookup runs here.
    """
    if facts is None:
        facts = profile.stored(session, client.id)
    resolved = _resolved(session, fmt, client, angle, target)
    missing = tuple(
        req
        for req in fmt.requires
        if not _satisfied(req, facts, client, angle, resolved)
    )
    return Readiness(missing)


def _satisfied(
    req: Requirement,
    facts: dict[str, ClientFact],
    client: Client,
    angle: Angle | None,
    target: PitchTarget | None = None,
) -> bool:
    if req.source is Source.PROFIL:
        fact = facts.get(req.key)
        return bool(fact and fact.value.strip())
    holder: object | None = {
        Source.MANDAT: client,
        Source.IMPULS: angle,
        Source.EMPFAENGER: target,
    }[req.source]
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


def _fact_lines(facts: dict[str, ClientFact]) -> str:
    """The filled profile fields, in the order the page reads them, or "".

    Empty rather than an excuse, because the two readers of this block need
    different sentences when it is empty: the writer is told to claim nothing,
    the checker is told that every claim it finds is unbacked.
    """
    return "\n".join(
        f"{field.label}: {facts[field.key].value}"
        for field in profile.FIELDS
        if field.key in facts and facts[field.key].value.strip()
    )


def _facts_block(facts: dict[str, ClientFact]) -> str:
    """The deep-dive profile as the writing prompt carries it.

    Everything a format is allowed to state about the mandate as fact comes from
    here. A line that is not in this block is not a fact, it is a guess.
    """
    return (
        _fact_lines(facts)
        or "Noch nichts hinterlegt. Behaupte also nichts über den Mandanten."
    )


def _evidence_articles(session: Session, angle: Angle) -> list[Article]:
    """The impulse's stories, in the impulse's own order, capped after resolving.

    Two things the one-line version gets wrong, both silently. ``IN`` hands rows
    back in database order, so the order :func:`newspulse.angles.suggest` chose,
    which is the order it ranked them in, is lost in every prompt. And capping
    the id list before the query means an impulse whose first ids no longer
    resolve shows fewer stories than it has, rather than the first
    :data:`_MAX_EVIDENCE` that still exist.
    """
    ids = list(dict.fromkeys(angle.article_ids or []))
    if not ids:
        return []
    rows = {
        row.id: row
        for row in session.scalars(select(Article).where(Article.id.in_(ids)))
    }
    return [rows[article_id] for article_id in ids if article_id in rows][
        :_MAX_EVIDENCE
    ]


def _evidence_block(session: Session, angle: Angle) -> str:
    """The headlines and feed snippets under the impulse, and nothing else.

    This function is where the Leistungsschutzrecht rule would break if it were
    going to. It cannot: ``articles`` has no body column, so the two fields read
    here are the headline and the snippet the feed itself syndicated, and the
    snippet is cut at :data:`_MAX_SNIPPET_CHARS`.
    """
    lines: list[str] = []
    for article in _evidence_articles(session, angle):
        lines.append(f"- ({article.source}) {article.title}")
        snippet = (article.summary_text or "").strip()
        if snippet:
            lines.append(f"  Feed-Anriss: {snippet[:_MAX_SNIPPET_CHARS]}")
    # Built first, then asked about: an impulse can name ids that no longer
    # resolve, and the header below promises that what follows is what is known.
    # Promising it over an empty list is how a model concludes it may fill one.
    if not lines:
        return (
            "BELEGTE MELDUNGEN\nKeine. Der Text darf sich also auf keine "
            "Berichterstattung berufen."
        )
    return (
        "BELEGTE MELDUNGEN\n"
        "Schlagzeilen und Feed-Anrisse, mehr war nie zu sehen. Was hier nicht "
        "steht, ist über diese Artikel nicht bekannt.\n" + "\n".join(lines)
    )


def _speaker(fmt: FormatDef, facts: dict[str, ClientFact]) -> str:
    """The person a quote may be attributed to, exactly as the profile holds it."""
    if not fmt.speaker_key:
        return ""
    fact = facts.get(fmt.speaker_key)
    return fact.value.strip() if fact else ""


def _guide_items(text: str) -> list[str]:
    """The guide's smallest readable units: one line, one bullet, one sentence.

    All three, because a guide is written all three ways. The tool's own
    :func:`newspulse.guide.distill` puts the No-Gos on one line separated by "·";
    a consultant editing the field by hand writes them as a list of lines or as
    three sentences after the label.
    """
    items: list[str] = []
    for line in _lines(text):
        for chunk in line.split("·"):
            items.extend(part.strip() for part in _sentences(chunk) if part.strip())
    return items


def nogos(client: Client) -> tuple[str, ...]:
    """What the mandate's guide says it never says.

    Read out of the guide rather than out of a field of their own, because that is
    where the consultant writes them and a second field would be the one nobody
    fills.

    The whole block, not the one sentence carrying the label. "No-Gos: Keine
    Heilversprechen. Nie über Preise sprechen. Keine Vergleiche mit
    Wettbewerbern." is three No-Gos, and keeping only the first refuses a Q&A that
    asks about either of the other two, which is a Q&A that does exactly what it
    was asked to do. So collection starts at the marker and runs to the next
    labelled section.

    Where a guide is a paragraph of prose with no further label, that runs to the
    end of it. The over-collection is deliberate and bounded: extra terms can only
    make the Q&A check pass, :data:`_MAX_NOGO_TERMS` counts from the marker
    outwards, and a wrongly refused Q&A costs a text while a wrongly accepted one
    costs a second read.

    A guide that names its No-Gos some other way, "Tabus:" or "Was wir nie
    sagen:", is recognised too, and a guide that names them in a way nothing here
    recognises leaves a log line. That case is otherwise invisible: the Q&A's
    central check simply does not run, no fault is raised, and the text reads as
    though it had been held to the guide.
    """
    text = (getattr(client, "comms_guide", "") or "").strip()
    collected: list[str] = []
    collecting = False
    for item in _guide_items(text):
        folded = item.casefold()
        if any(marker in folded for marker in _NOGO_MARKERS):
            collecting = True
        elif collecting and _GUIDE_LABEL.match(item):
            collecting = False
        if collecting:
            collected.append(item)
    if text and not collected:
        _log.info(
            "no No-Go block found in the guide for %r; the Q&A is written but not "
            "held to any No-Go",
            getattr(client, "name", ""),
        )
    return tuple(collected)


def _headlines(session: Session, target: PitchTarget | None) -> tuple[str, ...]:
    """The bylined headlines a briefing may name, and no others.

    Looked up once and handed to both halves that need them: the prompt, which
    lists them as the only pieces the briefing may refer to, and the validator,
    which holds the finished briefing to that same list. Two lookups would be two
    lists the moment a sweep landed between them, and the second one is the one
    the text would be refused against.
    """
    if target is None or not target.journalist:
        return ()
    return tuple(pitch.recent_headlines(session, target.journalist, target.outlet))


def _recipient_block(target: PitchTarget | None, headlines: tuple[str, ...]) -> str:
    """Who is asking, and what they published lately.

    The headlines come from :func:`newspulse.pitch.recent_headlines`, off the same
    coverage the pitch list is built from. A briefing that names a piece the
    journalist did not write is worse than one that names none: it is read out
    loud in the first minute of the interview.
    """
    if target is None:
        return (
            "WER FRAGT\n"
            "Kein Medium belegt. Erfinde weder Redaktion noch Namen."
        )
    lines = [f"Medium: {target.outlet}"]
    if target.journalist:
        lines.append(f"Journalist/in: {target.journalist}")
        if headlines:
            lines.append("Zuletzt von dieser Person, nur diese Schlagzeilen belegt:")
            lines.extend(f"- {headline}" for headline in headlines)
        else:
            lines.append(
                "Keine Schlagzeilen dieser Person belegt. Sag im Briefing, dass "
                "nichts vorliegt, statt etwas zu vermuten."
            )
    else:
        lines.append(
            "Kein Name belegt, nur die Redaktion. Erfinde keine:n Journalist:in."
        )
    if target.about_client:
        lines.append(
            f"Hat in den letzten Monaten {target.about_client}× über den Mandanten "
            "geschrieben."
        )
    else:
        lines.append("Hat über diesen Mandanten noch nie geschrieben.")
    return "WER FRAGT\n" + "\n".join(lines)


def _today_date() -> dt.date:
    """Today in the mandate's own zone, which is what a dateline is stamped with.

    UTC would date a release written at one in the morning to the day before the
    desk receives it, and the validator holds the dateline to this same answer.
    """
    return dt.datetime.now(config.local_zone()).date()


def today(*, day: dt.date | None = None) -> str:
    """Today, written the way a dateline writes it: "20. August 2026".

    A slot rather than an example in the prompt file. The release's dateline is a
    hard part of its contract, its date is the one input no profile field holds,
    and :func:`_refusal_block` forbids inventing a date in the same prompt. Left
    to an illustrative "Berlin, 20. August 2026" the model copies the example and
    every release the tool ever writes carries the day this line was typed.

    In the mandate's own zone rather than UTC, because a release written at one in
    the morning is dated the day the desk receives it.
    """
    date = day or _today_date()
    return f"{date.day}. {_DE_MONTHS[date.month - 1]} {date.year}"


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
    session: Session,
    fmt: FormatDef,
    client: Client,
    angle: Angle,
    *,
    facts: dict[str, ClientFact] | None = None,
    target: Recipient = _UNRESOLVED,
    headlines: tuple[str, ...] | None = None,
) -> str:
    """Render one format's prompt. Every format gets the same blocks.

    One shared slot set rather than one per format: a prompt file uses what it
    needs and ignores the rest, which is what lets a seventh format be a file
    somebody writes rather than a change here.

    ``headlines`` is the byline's recent work when the caller already holds it,
    the same courtesy ``facts`` and ``target`` are. :func:`write` holds it because
    the validator is held to the same list.
    """
    if facts is None:
        facts = profile.stored(session, client.id)
    resolved = _resolved(session, fmt, client, angle, target)
    if headlines is None:
        headlines = _headlines(session, resolved)
    return fmt.template().substitute(
        recipient=_recipient_block(resolved, headlines),
        today=today(),
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
        # A reply that volunteers a brain_version would otherwise be validated
        # into the field and the row would carry a number nothing recorded.
        draft = AssetDraft.model_validate(without_provenance(payload))
    except Exception as exc:  # noqa: BLE001 — pydantic raises its own type
        raise ParseError(f"the format reply did not match the schema: {exc}") from exc
    if not draft.body.strip():
        raise ParseError("the format reply carried no text")
    return draft


def _attributed(
    draft: AssetDraft, fmt: FormatDef, facts: dict[str, ClientFact]
) -> AssetDraft:
    """The draft with its attribution taken from the profile, not from the reply.

    The prompt asks the model to copy the name verbatim and the model mostly does.
    "Mostly" is not a guarantee, and the thing it is not a guarantee about is a
    named person's quote, which is the one artefact here that cannot be repaired
    after it has gone out. So the reply's answer is not kept: the format named a
    profile field, that field is on file because :func:`requirements_met` said so,
    and its value is what is stored.

    A format that names no field quotes nobody, and its attribution is empty
    rather than whatever the model found in the free-text profile block.
    """
    named = _speaker(fmt, facts)
    returned = draft.speaker.strip()
    if returned and returned != named:
        # Worth a line in the log: the body was written by the same call and may
        # carry the same wrong name in its quote, which FMT-02's validator reads.
        _log.warning(
            "%s: attribution %r replaced with the profile's %r",
            fmt.key,
            returned,
            named,
        )
    return draft.model_copy(update={"speaker": named})


def validate(fmt: FormatDef, draft: AssetDraft, given: Given) -> list[str]:
    """What this draft fails to carry of the structure its format declared.

    Empty is the good answer. A format with no validator has its contract stated
    in the prompt and taken on trust, which is what a seventh format starts as and
    what none of the six is.
    """
    if fmt.validator is None:
        return []
    return fmt.validator(draft, given)


def _correction(fault: str) -> str:
    """What the second attempt is told about the first one.

    The analyzer retries a batch with the identical prompt, and for a
    classification that is right: the failure there is a mangled envelope. Here
    the failure is a text that missed its shape, and re-asking the same question
    the same way is how the same shape comes back. So the complaint travels: it is
    already a sentence, it already names what is missing, and it costs nothing.
    """
    if not fault:
        return ""
    complaint = fault if len(fault) <= _MAX_FAULT_CHARS else f"{fault[:_MAX_FAULT_CHARS]} …"
    return (
        "\n\nDER VORIGE VERSUCH WURDE ABGELEHNT\n"
        f"{complaint}\n"
        "Schreibe den Text neu, vollständig, mit genau der oben verlangten "
        "Struktur. Erfinde nichts dazu, um eine Lücke zu füllen."
    )


def _given(
    fmt: FormatDef,
    client: Client,
    facts: dict[str, ClientFact],
    target: PitchTarget | None,
    headlines: tuple[str, ...],
) -> Given:
    """Everything the validators may hold the finished text against.

    Every field here was in the prompt that wrote the text. That is the rule the
    whole contract rests on: a validator may ask whether the release copied the
    seat and the date it was handed, and may not ask whether the news is true.
    """
    seat = facts.get(_SEAT_KEY)
    return Given(
        speaker=_speaker(fmt, facts),
        nogos=nogos(client),
        recipient=target,
        seat=seat.value.strip() if seat else "",
        day=_today_date(),
        headlines=headlines,
    )


def _drafted(
    fmt: FormatDef,
    prompt: str,
    given: Given,
    facts: dict[str, ClientFact],
    *,
    invoke: Callable[..., str],
    label: str,
) -> AssetDraft:
    """Two attempts at a draft that carries the format's structure, then a refusal.

    One retry, then give up, which is the analyzer's rule for a batch and for the
    same reason: the second failure is evidence about the model rather than about
    the weather. What is different here is that giving up is not silent. A batch
    that drops is a story missing from a list nobody counted; a format that fails
    is a button somebody pressed and is standing in front of, so the refusal
    carries the reason it refused.

    Only a bad answer is retried, not a backend that could not answer. A
    :class:`newspulse.analyzer.BackendError` means the provider is unreachable or
    timed out, ``invoke_with_fallback`` has already tried the other one by then,
    and a third attempt spends another timeout to learn the same thing.
    """
    fault = ""
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            raw = invoke(prompt + _correction(fault), timeout=config.ANALYZER_TIMEOUT)
            draft = _attributed(_parse(raw), fmt, facts)
        except ParseError as exc:
            # The parser's own message keeps its offsets and its English, in the
            # log, where they are what somebody debugging this needs. What travels
            # onwards into the retry and the refusal is a sentence.
            _log.warning("%s for %r: unreadable reply: %s", fmt.key, label, exc)
            fault = _UNREADABLE
        else:
            faults = validate(fmt, draft, given)
            if not faults:
                return draft
            fault = " ".join(faults)
        if attempt < _MAX_ATTEMPTS:
            _log.warning(
                "%s for %r missed its structure (attempt %d/%d): %s; retrying",
                fmt.key, label, attempt, _MAX_ATTEMPTS, fault,
            )
            continue
        _log.error(
            "%s for %r gave up after %d attempts: %s; nothing stored",
            fmt.key, label, _MAX_ATTEMPTS, fault,
        )
        raise Malformed(fmt, fault)
    raise AssertionError("unreachable")  # pragma: no cover - keeps the checker honest


def write(
    session: Session,
    fmt: FormatDef,
    client: Client,
    angle: Angle,
    *,
    invoke: Callable[..., str] = invoke_with_fallback,
    note: Callable[[str], None] | None = None,
) -> AssetDraft:
    """Write ``fmt`` for this mandate off this impulse.

    Raises :class:`RequirementsMissing` before spending a model call when the
    format's declared inputs are not on file. That is DEC-2: nothing is written
    and the reason names the field. Raises :class:`Malformed` when the model
    answered twice without carrying the structure the format declares, rather than
    handing back a text that is missing its quote or its boilerplate.

    Note what is *not* here: no branch on which format this is. The definition
    carries the requirements, the structure, the prompt and the validator, so this
    function is the same code for the first format and for the seventh.

    The returned draft's ``speaker`` is the profile's, not the model's. See
    :func:`_attributed`.

    ``note`` receives the reason whenever nothing is written, the way
    :func:`newspulse.angles.suggest` reports its silence. A refusal the reader can
    read is a result; a silent one is indistinguishable from a broken button, and
    this one has a person waiting in front of it.

    ``invoke`` is injectable so the tests drive the whole path, prompt to stored
    row, without a subprocess.
    """
    facts = profile.stored(session, client.id)
    target = _resolved(session, fmt, client, angle, _UNRESOLVED)
    readiness = requirements_met(session, fmt, client, angle, facts=facts, target=target)
    if not readiness.ok:
        _log.info("%s refused for %r: %s", fmt.key, client.name, readiness.reason)
        refused = RequirementsMissing(fmt, readiness)
        _tell(note, str(refused))
        raise refused
    headlines = _headlines(session, target)
    prompt = prompt_for(
        session, fmt, client, angle, facts=facts, target=target, headlines=headlines
    )
    # Captured beside the prompt it describes, not after the call: an override
    # saved while the model is thinking belongs to the next text, and a stamp
    # read afterwards would file this one under standards it never saw.
    written_under = brain.version(session)
    try:
        drafted = _drafted(
            fmt,
            prompt,
            _given(fmt, client, facts, target, headlines),
            facts,
            invoke=invoke,
            label=client.name,
        )
        return drafted.model_copy(update={"brain_version": written_under})
    except Malformed as exc:
        _tell(note, str(exc))
        raise


def _tell(note: Callable[[str], None] | None, reason: str) -> None:
    """Hand the refusal to whoever is storing reasons, if anyone is."""
    if note is not None:
        note(reason)


# --- The checks, shared by every format ----------------------------------------


@dataclass(frozen=True, slots=True)
class Checkable:
    """A finished text plus everything the model that wrote it was allowed to see.

    The checkers take this rather than an ``Asset`` or an ``Outreach`` row, which
    is what lets the letter and the six formats go through the same code: what a
    check needs is a text, the position it argues, and everything that was
    provable when it was written, and all seven have those.

    "Everything provable" is two blocks, not one, and both are load-bearing. The
    stories under the impulse settle whether a claim about the coverage holds up;
    the mandate's own facts settle whether a claim about the mandate does. Give a
    checker only the first and it has to treat a quote from the profile's named
    spokesperson exactly like an invented one.
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
    #: What the writer was told about the mandate itself, already rendered. The
    #: checker's first question is whether a claim about the mandate is backed,
    #: and for a spokesperson's name, a headcount or a founding year the answer
    #: is only in here. Without it a profile-backed quote and a fabricated one
    #: look exactly alike, which is the one comparison this check exists for.
    profile_facts: str = ""

    @property
    def readable(self) -> tuple[str, str]:
        """Title and body as they will be stored, which is what a checker reads.

        Both storage paths put title and body through :func:`newspulse.prose.plain`,
        and a verdict quoting a sentence that the house-style rewrite has since
        changed sends the reader looking for words that are not on the page. Both
        checkers take these, so neither can quote something the other cannot see.
        """
        return prose.plain(self.title), prose.plain(self.body)


#: Every way a check can fail to produce a verdict, as one tuple used by all three
#: places that have to survive one. :class:`AnalyzerError` rather than
#: ``ParseError`` because it is the base of both that and
#: :class:`newspulse.analyzer.BackendError`, and ``BackendError`` is what
#: :func:`newspulse.gemini.generate` raises for *every* realistic failure — host
#: unreachable, timeout, HTTP error, empty completion. It is not a
#: ``RuntimeError``, so a handler naming only those caught the rare case and let
#: the common one through. ``RuntimeError`` stays for
#: :func:`newspulse.gemini.reviewer` refusing on an unconfigured second model, and
#: ``OSError`` for the socket underneath.
_CHECK_FAULTS = (AnalyzerError, RuntimeError, OSError)


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
    #: Why the guide check produced no verdict, when it produced none.
    guide_note: str = ""
    #: Whether that was a malfunction rather than a mandate with no guide. The two
    #: are stored differently on purpose: having nothing to check against is a
    #: state of the mandate, and a check that ran and broke is a text nobody read.
    guide_failed: bool = False


def crosscheck(
    session: Session,
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

    # The text as it will be stored, not as it came back. See Checkable.readable.
    title, body = item.readable

    prompt = _template(_CROSSCHECK_RESOURCE).substitute(
        client=client.name,
        kind=item.kind,
        thesis=item.thesis or "nicht ausformuliert",
        overclaim=item.overclaim or "nicht ausformuliert",
        evidence=item.evidence,
        profile_facts=item.profile_facts.strip() or _NO_FACTS,
        title_label=item.title_label,
        title=title,
        body=body,
    )
    written_under = brain.version(session)
    raw = generate(prompt)
    try:
        # Provenance is not the model's to state: a reply that volunteers a
        # brain_version would otherwise be validated into the field, or fail
        # validation and cost the whole check.
        payload = without_provenance(json.loads(strip_code_fence(raw)))
        review = MessageReview.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 — pydantic and json raise their own
        raise ParseError(f"crosscheck did not match the schema: {exc}") from exc
    review = review.model_copy(update={"brain_version": written_under})

    # One thing the checker cannot be trusted to catch, because it is mechanical:
    # the house rule on dashes. Read off the draft rather than the rewritten text,
    # since by then there is nothing left to find. Prepended rather than appended
    # so the cap drops one of the model's judgements instead of the one finding
    # here that is not a judgement at all.
    if prose.has_dash(item.body) or prose.has_dash(item.title):
        review = review.model_copy(
            update={"concerns": [_DASH_CONCERN, *review.concerns][:MAX_CONCERNS]}
        )
    return review, config.review_model()


def check(
    session: Session,
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

    Two verdicts also means two failures, and neither takes the other down with
    it. The crosscheck has been paid for by the time the guide check runs, so a
    guide pass that breaks leaves a :class:`Checked` carrying the verdict that did
    arrive and a note saying the other one did not. Discarding a completed check
    because a second one failed would spend a model call to end up with less than
    the caller had a line earlier.
    """
    review, reviewed_by = crosscheck(session, client, item, generate=generate)
    # The same strings the crosscheck read, and the same ones store() will write.
    # A breach quotes the sentence it objects to and that quote is stored verbatim,
    # so a guide check reading the raw reply produces an objection whose quoted
    # half is not on the page whenever the house-style rewrite touched it.
    title, body = item.readable
    try:
        verdict = guide.check_guide(
            client,
            title=title,
            body=body,
            kind=item.kind,
            generate=guide_generate if guide_generate is not None else generate,
        )
    except _CHECK_FAULTS:
        # ERROR rather than WARNING: nothing read this text against the mandate's
        # own rules, and that is the check whose absence costs a mandate.
        _log.error("guide check failed for %r", client.name, exc_info=True)
        return Checked(
            review=review,
            reviewed_by=reviewed_by,
            guide_note=guide.CHECK_FAILED,
            guide_failed=True,
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
    session: Session,
    fmt: FormatDef,
    angle: Angle,
    draft: AssetDraft,
    *,
    facts: dict[str, ClientFact] | None = None,
) -> Checkable:
    """The finished draft, packed for :func:`check` with everything that backs it.

    "Everything" is both halves. The stories under the impulse say whether a
    claim about the coverage is provable; the profile says whether a claim about
    the mandate is. The formats that quote a named person are written *from* the
    profile, so a checker given only the headlines has to treat a correctly
    attributed quote and an invented one identically, and it is told to call the
    invented one the worst mistake there is.

    ``facts`` is the mandate's profile when the caller already holds it, which
    :func:`write` does.
    """
    if facts is None:
        facts = profile.stored(session, angle.client_id)
    return Checkable(
        kind=f"ein Text im Format {fmt.name}",
        title_label=fmt.title_label,
        title=draft.title,
        body=draft.body,
        thesis=angle.thesis,
        overclaim=angle.overclaim,
        evidence=_evidence_block(session, angle),
        profile_facts=_fact_lines(facts),
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
        # The checker's own flag is not trusted against its own findings, exactly
        # as guide.check_guide does not trust it against its breaches. A verdict
        # that lists an objection and still says send would render this row as
        # clean, and the objection would sit on it unread. That includes the one
        # finding the checker never made: the mechanical dash concern, which is
        # added after the model has already answered.
        row.review_ok = checked.review.send and not concerns

    if checked is None or checked.guide is None:
        # A mandate with no guide leaves a note and no reviewer, so the page can
        # say the check could not run instead of showing an empty objection list.
        # A check that ran and broke is not allowed to read as clean on top of
        # that: it is the state this whole column exists to keep visible.
        row.guide_review = checked.guide_note if checked else ""
        row.guide_reviewed_by = ""
        row.guide_review_ok = not (checked and checked.guide_failed)
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

    The surface never asks for that second row: every control that would write a
    format refuses once the format is released, so it can only be reached by a
    release landing while a worker is inside its model call. When it is,
    :func:`current` keeps the released row standing, which is what stops that
    race from quietly demoting a released text back to a draft.

    Deliberately not the place the structural contract is enforced. :func:`write`
    has already refused anything that missed it, and the other text that arrives
    here is one a human edited. A consultant who deletes the boilerplate because
    this release is not going out with one has made an editorial decision, and a
    tool that answered it by refusing to save his work would be wrong twice: about
    the text, and about who is accountable for it.
    """
    row = _replaceable(session, angle.id, fmt.key) or Asset(
        client_id=client.id, angle_id=angle.id, kind=fmt.key
    )
    # House style, enforced rather than requested: the prompts ask for no dashes
    # and the model relapses by the third paragraph. One call site for all seven
    # formats. See newspulse.prose.
    row.title = prose.plain(draft.title)
    row.body = prose.plain(draft.body)
    # The attribution too: it is printed under the text, and a profile field
    # somebody typed with a dash would otherwise be the one line on the page the
    # house rule never reached.
    row.speaker = prose.plain(draft.speaker).strip()
    row.brain_version = brain.stamp(draft.brain_version, what="this text")
    row.generated_at = dt.datetime.now(dt.UTC)
    # The model's words, freshly. Whatever a human had done to the previous draft
    # was done to a different text.
    row.edited_at = None
    _apply_checks(row, checked)
    session.add(row)
    session.commit()
    return row


def produce(
    session: Session,
    fmt: FormatDef,
    client: Client,
    angle: Angle,
    *,
    invoke: Callable[..., str] = invoke_with_fallback,
    generate: Callable[..., str] | None = None,
    guide_generate: Callable[..., str] | None = None,
    note: Callable[[str], None] | None = None,
) -> Asset:
    """Write one format, have it read, and store it: the whole act, once.

    The surface asks for a text, not for three steps, and the three steps have an
    order that must not be got wrong twice. Composed here rather than in the route
    that starts the worker, because a second surface calling it — the daily sweep,
    a crisis in PR-06 — would otherwise recompose it from memory and get the
    fault isolation below subtly different.

    That isolation is the part worth stating. A check that cannot run must not
    lose the text that has already been paid for: the model call is spent, the
    draft is good, and the honest answer is a stored text that says plainly that
    nothing read it. :func:`newspulse.models.Asset.check_state` renders exactly
    that, and never as a clean bill of health.

    Raises whatever :func:`write` raises: nothing is stored when the format was
    refused for a missing field or gave up on its structure, so a failed run
    leaves the previous text for this format standing untouched.
    """
    draft = write(session, fmt, client, angle, invoke=invoke, note=note)
    checked: Checked | None = None
    try:
        checked = check(
            session,
            client,
            checkable(session, fmt, angle, draft),
            generate=generate,
            guide_generate=guide_generate,
        )
    except _CHECK_FAULTS as exc:
        _log.warning("%s for %r went unchecked: %s", fmt.key, client.name, exc)
        _tell(
            note,
            f"{fmt.name}: Der Text steht, aber das Zweitmodell hat ihn nicht "
            f"gegengelesen: {exc}",
        )
    return store(session, fmt, client, angle, draft, checked)


# --- What state one stored text is in ------------------------------------------


class AssetState(StrEnum):
    """Where one format stands on one impulse, as the surface shows it.

    Four states because the surface owes four answers, and the first two are the
    pair that matters: a format nobody has written and a draft nobody has read
    are both "not ready", and a strip that drew them alike would hide the six
    unwritten formats behind the one that exists.
    """

    UNGESCHRIEBEN = "ungeschrieben"
    ENTWURF = "entwurf"
    GEPRUEFT = "geprueft"
    FREIGEGEBEN = "freigegeben"


class Refused(RuntimeError):
    """An act the asset's own state does not allow.

    One exception rather than one per rule: what the caller does with any of them
    is the same thing, which is to show the sentence to the person who pressed the
    button. The message is that sentence, already in German.
    """


#: Why a released text takes no more edits. The letter's rule, arriving here for
#: the same reason it exists there: a released text is the record of what actually
#: went out, and a record that can be rewritten afterwards is not a record.
RELEASED_IS_FINAL = (
    "Dieser Text ist freigegeben. Freigegebene Texte werden weder geändert noch "
    "ersetzt, weil sie festhalten, was tatsächlich hinausgegangen ist."
)

#: Why an edited text cannot be released yet. The whole reason editing is a
#: separate act here: the verdicts on screen belonged to the words the model
#: wrote, and a text a human changed after the check was cleared is a text
#: nothing has read.
UNREAD_SINCE_EDIT = (
    "Der Text wurde nach der Prüfung von Hand geändert. Vor der Freigabe muss "
    "ihn das Zweitmodell noch einmal lesen."
)

#: What is stored on a text whose check was attempted and could not run, in the
#: field the verdict would have gone in. On the row rather than in a note in
#: memory, because it is a fact about this text and has to be on screen at the
#: reader's next visit, which may be after a restart.
#:
#: It exists so that an edited text does not become permanently unreleasable on an
#: installation with no second model: the alternative was the tool blocking the
#: release exactly where a human had taken responsibility for the words, while
#: still permitting it on an unchecked draft only a model had ever seen. The
#: sentence says what the reader is deciding.
#:
#: Stored as a line of its own, with whatever the backend said underneath it. The
#: two belong to different readers: this sentence is the consultant's and is
#: translated, and the exception text is the operator's and is whatever the
#: backend wrote. Run together as one interpolated string neither worked — the
#: page printed the lot raw, so the English entry for this sentence could never
#: be looked up and an English reader read the German.
CHECK_UNAVAILABLE = (
    "Das Zweitmodell konnte diesen Text nicht lesen. Er ist ungeprüft — "
    "wer ihn jetzt freigibt, gibt ihn ungelesen frei."
)

#: Why an edit posted from a page the text has moved on from is not stored.
#: :func:`store` reuses the row when it replaces a draft, so an edit panel opened
#: before a re-write posts back to the same id and would put the words the reader
#: was looking at over the ones the model has since written — and stamp the
#: result ``edited_at``, which reads as a human having approved them.
STALE_EDIT = (
    "Der Text wurde neu geschrieben, während dieses Formular offen war. Die "
    "Änderung wurde nicht gespeichert, damit sie den neuen Text nicht "
    "überschreibt. Bitte den jetzt angezeigten Text bearbeiten."
)


def state_of(asset: Asset | None) -> AssetState:
    """The state one format is in on one impulse. ``None`` means unwritten."""
    if asset is None:
        return AssetState.UNGESCHRIEBEN
    if asset.released:
        return AssetState.FREIGEGEBEN
    if asset.check_state is CheckState.GEPRUEFT:
        return AssetState.GEPRUEFT
    return AssetState.ENTWURF


def _read_since_edit(asset: Asset) -> bool:
    """Whether anything at all has been recorded about this text since the edit.

    :func:`edit` blanks all four verdict fields, so every one of them is empty on a
    freshly edited row and any of them being filled means something has since
    happened to it. Two things can fill them and both count: a verdict, and a
    check that was attempted and could not run (:data:`CHECK_UNAVAILABLE`).

    Counting the second one is the point. "The check has not run yet" and "the
    check could not run" are different facts, and only the first is a reason to
    keep the release control out of reach. Neither reads as clean: ``check_state``
    still wants a reviewer's name before it says GEPRUEFT, so a text nothing could
    read stays visibly unchecked and merely becomes releasable by a human who is
    told so.
    """
    return bool(
        asset.reviewed_by
        or asset.guide_reviewed_by
        or asset.review
        or asset.guide_review
    )


def needs_recheck(asset: Asset) -> bool:
    """Whether a human changed this text and nothing has looked at it since.

    Readable off the row because :func:`edit` clears the verdicts as it stores the
    edit: a stored verdict always belongs to the text beside it, so after an edit
    there is no verdict, and "edited and unread" is exactly those two facts
    together. No second timestamp column, and no way for the two to disagree.
    """
    return asset.edited_at is not None and not _read_since_edit(asset)


def releasable(asset: Asset) -> bool:
    """Whether the release control may be offered for this text at all."""
    return not asset.released and not needs_recheck(asset)


def _stamp(when: dt.datetime | None) -> str:
    return when.isoformat() if when is not None else ""


def version(asset: Asset) -> str:
    """Which text this is, as one token an edit form can post back.

    Both timestamps, because both acts replace the words in the box: a re-write
    stamps ``generated_at``, a save stamps ``edited_at``. The row id says nothing
    about either — :func:`store` reuses the row when it replaces a draft — so the
    id is exactly what a stale form and a fresh one have in common.
    """
    return f"{_stamp(asset.generated_at)}/{_stamp(asset.edited_at)}"


def edit(
    session: Session, asset: Asset, *, title: str, body: str, seen: str = ""
) -> Asset:
    """Store a human's edit, and mark the text as one a human has been through.

    Two effects, and the second is the point. The edit is kept, and both verdicts
    are dropped, because they were given on the words the model wrote. Leaving
    them would put a green "keine Einwände" over a paragraph the checker never
    saw, which is the one thing this whole column exists to prevent.

    A released text is refused: its words are the record of what went out. So is
    an edit whose ``seen`` token no longer matches :func:`version` — the text
    moved on while the panel was open, and saving would put the old words back
    under a human's timestamp. An empty ``seen`` is a caller that does not carry
    the token and gets the rule it had before: the check is a guard on the form,
    not a second lock on the row.
    """
    if asset.released:
        raise Refused(RELEASED_IS_FINAL)
    if seen and seen != version(asset):
        raise Refused(STALE_EDIT)
    # The house rule reaches a human's words too. It is a rule about what leaves
    # the building, not about who typed it, and a page that enforced it on the
    # model's paragraph and not on the one edited underneath would ship the tell
    # it exists to remove. See newspulse.prose.
    asset.title = prose.plain(title)
    asset.body = prose.plain(body)
    asset.edited_at = dt.datetime.now(dt.UTC)
    _apply_checks(asset, None)
    session.add(asset)
    session.commit()
    return asset


def recheck(
    session: Session,
    client: Client,
    asset: Asset,
    *,
    generate: Callable[..., str] | None = None,
    guide_generate: Callable[..., str] | None = None,
) -> Asset:
    """Read a stored text again, exactly as it now stands.

    The same two checks a freshly written one goes through, over the stored title
    and body rather than a draft, which is what makes it worth running after an
    edit: what it reads is what the consultant will send.

    A check that cannot run is recorded rather than raised, the same isolation
    :func:`produce` has and for a sharper reason. Leaving the row untouched left
    an edited text with both halves of :func:`needs_recheck` true forever, so on
    an installation with no second model a consultant could write and edit but
    never release — the tool refusing the one person accountable for the words.
    What is stored instead is :data:`CHECK_UNAVAILABLE`, which is an answer and is
    not a clean one: no reviewer's name is written, so ``check_state`` still says
    UNGEPRUEFT and the page still draws the text as unread.
    """
    angle = session.get(Angle, asset.angle_id)
    if angle is None:  # pragma: no cover - the FK cascades, so only a race
        raise Refused("Der Impuls zu diesem Text existiert nicht mehr.")
    fmt = definition(asset.kind)
    item = checkable(
        session,
        fmt,
        angle,
        AssetDraft(title=asset.title, body=asset.body, speaker=asset.speaker),
    )
    try:
        _apply_checks(
            asset,
            check(session, client, item, generate=generate,
                  guide_generate=guide_generate),
        )
    except _CHECK_FAULTS as exc:
        _log.warning("re-check of %s for %r failed: %s", asset.kind, client.name, exc)
        _apply_checks(asset, None)
        # The sentence on its own line, the backend's words under it. The page
        # renders this field line by line through the translator, so the reader's
        # half is looked up and the operator's half passes through as written.
        asset.review = f"{CHECK_UNAVAILABLE}\n{exc}"
        # Never ok: an unread text must draw with the warning dot, not the clean
        # one, whatever the reader then decides to do about it.
        asset.review_ok = False
    session.add(asset)
    session.commit()
    return asset


#: What fits in ``assets.released_by``. Cut here rather than at the database,
#: which would raise on a long operator name instead of releasing the text.
_MAX_RELEASER_CHARS = 80


def release(session: Session, asset: Asset, by: str = "") -> Asset:
    """Record the human release, the way the outreach ledger records a letter's.

    Grade F without exception: nothing leaves this tool because a model was
    content with it. What is stored is who and when, so the release is a trace
    rather than a state change nobody signed.

    Releasing twice is not an error, it is the second click on a button that had
    not repainted yet; the first release stands, timestamp and all.
    """
    if asset.released:
        return asset
    if needs_recheck(asset):
        raise Refused(UNREAD_SINCE_EDIT)
    asset.released_at = dt.datetime.now(dt.UTC)
    asset.released_by = by.strip()[:_MAX_RELEASER_CHARS]
    session.add(asset)
    session.commit()
    return asset


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


def current(session: Session, angle_ids: list[int]) -> dict[int, dict[str, Asset]]:
    """The text that stands for each format on each impulse, keyed twice.

    "Stands" is the released row of that kind if one exists and the newest row
    otherwise, which is the one the strip shows and the one the buttons act on.
    There can be more than one row — a released text keeps its row when a draft
    is written beside it — and a released one is never the loser of that pair:
    it is the record of what went out, and the strip is where the immutability
    rule is enforced.
    """
    return {
        angle_id: _standing_per_kind(rows)
        for angle_id, rows in by_angle(session, angle_ids).items()
    }


def _standing_per_kind(rows: list[Asset]) -> dict[str, Asset]:
    """What stands for each kind: the released row if there is one, else the newest.

    Released beats recent, and that is the immutability rule surviving a race
    rather than a preference. A release that lands while a re-write of the same
    format is inside its model call leaves :func:`store` with no replaceable
    draft, so the worker's text is inserted *beside* the released row and is the
    newer of the two. Picked on recency alone, the released text would drop off
    the strip, the package would count nothing as released, and every control
    that refuses on a released row — re-write, re-check, "Fehlende schreiben" —
    would go live again over the record of what actually went out.

    ``rows`` arrives newest first, so the newest row of a kind is taken and only
    a released one displaces it.
    """
    per: dict[str, Asset] = {}
    for row in rows:
        standing = per.get(row.kind)
        if standing is None or (row.released and not standing.released):
            per[row.kind] = row
    return per


__all__ = [
    "CHECK_UNAVAILABLE",
    "FORMATS",
    "GASTBEITRAG_CHARS",
    "MAX_TALKING_POINTS",
    "REGISTRY",
    "RELEASED_IS_FINAL",
    "STALE_EDIT",
    "UNREAD_SINCE_EDIT",
    "AssetState",
    "Checkable",
    "Checked",
    "FormatDef",
    "Given",
    "Malformed",
    "Readiness",
    "Refused",
    "Requirement",
    "RequirementsMissing",
    "Source",
    "Validator",
    "by_angle",
    "check",
    "checkable",
    "crosscheck",
    "current",
    "definition",
    "edit",
    "for_angle",
    "needs_recheck",
    "nogo_terms",
    "nogos",
    "produce",
    "prompt_for",
    "recheck",
    "recipient",
    "release",
    "releasable",
    "requirements_met",
    "state_of",
    "store",
    "today",
    "validate",
    "version",
    "write",
]
