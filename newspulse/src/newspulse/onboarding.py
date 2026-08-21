"""The twenty kick-off questions, and the store that holds their answers.

`profile.research()` reads the open web and fills fourteen fields well.
`guide.distill()` reads an uploaded brand book. Both are limited the same way:
they can only find what the client has already published. A website never says
which sentence would end the relationship if it appeared in print, which
competitor claim is a lie the client can disprove, or which topic legal has ruled
out until the case closes. Those answers exist in the kick-off conversation and
nowhere else, and today they evaporate when the call ends.

Three rules shape this module.

**The questions are data.** They will be edited by whoever reads the answers they
produce, and a question that has to be found in a Jinja file will not be edited.
Same reason ``profile.FIELDS`` is data.

**Every question declares what it feeds.** A questionnaire whose answers go
nowhere in particular is a form; one where each question names its target is an
input layer. The declaration is checked by the test suite, so a twenty-first
question cannot be added without saying where it goes.

**Nothing here adopts anything.** This module writes to ``onboarding_answers``
and to nothing else. Turning an answer into a profile fact, a no-go or a
comparison set is a separate, deliberate act, because an onboarding that silently
writes a client's own words into a guide is how a wrong sentence becomes policy.
The conversion at the bottom of this file therefore *offers*: profile proposals,
a guide draft and named competitors. Every one of them is a return value; the
only thing it writes is the record that the kick-off is where the material came
from.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import guide, profile, rivals
from .analyzer import invoke_with_fallback
from .models import ANSWERED_BY_DEFAULT, Client, OnboardingAnswer
from .schemas import RivalSuggestion

#: List answers are stored as one text column, one entry per line. A child table
#: for two or three names per mandate would buy nothing that ``splitlines`` does
#: not, and it would put the questionnaire's shape into the schema, where the
#: whole point is that the shape is editable code.
_ENTRY_SEPARATOR = "\n"

#: What the profile and the guide name as the origin of an adopted answer, and
#: the filename the answers are filed under as a guide source. Not a URL: nobody
#: published the kick-off call, and a provenance line linking nowhere is exactly
#: right — the person who said it is the source.
SOURCE_NAME = "Kickoff-Fragebogen"


class Target(StrEnum):
    """Where an answer is headed once someone accepts it.

    Named after places that exist in the tool, not after abstractions: a reader
    of the questionnaire can click through to every one of them.
    """

    PROFIL = "Profil"
    #: A rule the guide check holds every generated text against.
    NOGO = "No-Go"
    GUIDE = "Guide"
    VERGLEICHSGRUPPE = "Vergleichsgruppe"
    THEMENFELDER = "Themenfelder"
    KONTAKTE = "Kontakte"

    @property
    def verb(self) -> str:
        """How the page says it. A field gets *filled*; a rule *becomes*."""
        return "Wird" if self is Target.NOGO else "Füllt"


class InputKind(StrEnum):
    """How much room the answer needs, and whether there is more than one of it."""

    #: One line: a name, a company, a number.
    ZEILE = "zeile"
    #: A paragraph. Most of the questionnaire, because most of it is prose.
    ABSATZ = "absatz"
    #: Several of the same thing — spokespeople, journalists. Entered one at a
    #: time and shown as chips, so adding a fourth never means retyping three.
    LISTE = "liste"


class Progress(StrEnum):
    """A section's state in the rail, which is not a percentage but a shape."""

    OFFEN = "offen"
    TEILWEISE = "teilweise"
    FERTIG = "fertig"


@dataclass(frozen=True, slots=True)
class Feed:
    """One downstream target, and the named slot inside it.

    The slot is words rather than a key: the page says "Profil · Geschäftsfeld"
    to a consultant, and ONB-02 is what maps that to a column.
    """

    target: Target
    slot: str = ""


@dataclass(frozen=True, slots=True)
class Section:
    """A group of questions asked in one breath."""

    key: str
    #: The heading over the questions, phrased as the consultant would ask it.
    title: str
    #: The same section in the progress rail, where there is room for three words.
    short: str
    intro: str


@dataclass(frozen=True, slots=True)
class Question:
    """One question, and where its answer goes."""

    key: str
    section: str
    text: str
    help: str
    kind: InputKind
    #: Never empty. Enforced in :func:`_check_question_set` at import time and by
    #: the fixture test, because a question that feeds nothing is a form field.
    feeds: tuple[Feed, ...]
    #: The clause after the targets: "jeder Text wird dagegen geprüft".
    note: str = ""
    #: Greyed inside the empty field. Not a second copy of the help line — it
    #: states the *shape* of the answer ("Unternehmen, und in einem Halbsatz
    #: warum") where the help states why the question is asked. Empty where the
    #: locked mock leaves it empty.
    placeholder: str = ""

    @property
    def verb(self) -> str:
        """"Füllt" or "Wird", taken from the first target it feeds."""
        return self.feeds[0].target.verb

    @property
    def is_list(self) -> bool:
        return self.kind is InputKind.LISTE

    @property
    def is_prose(self) -> bool:
        """A paragraph rather than a line. Read by the template, which would
        otherwise compare the enum against the bare string ``'absatz'`` and fall
        silently through to a one-line input if the member were ever renamed."""
        return self.kind is InputKind.ABSATZ


SECTIONS: tuple[Section, ...] = (
    Section(
        "unternehmen",
        "Was das Unternehmen ist",
        "Was das Unternehmen ist",
        "Vier Fragen, die jeder Text braucht. Antworten hier ersetzen, was die "
        "Recherche geraten hat.",
    ),
    Section(
        "sagen",
        "Was gesagt werden darf, und was nie",
        "Sagen und schweigen",
        "Der Teil, den kein Modell erraten kann. Aus diesen Antworten entsteht "
        "der Guide, gegen den später jeder Text geprüft wird.",
    ),
    Section(
        "ziele",
        "Was erreicht werden soll",
        "Ziele",
        "Ohne Ziel ist jede Berichterstattung gleich viel wert, und das ist sie nie.",
    ),
    Section(
        "medien",
        "Medien und Beziehungen",
        "Medien und Beziehungen",
        "Wo Sie vorkommen müssen, und wen Sie dort schon kennen.",
    ),
    Section(
        "zusammenarbeit",
        "Zusammenarbeit",
        "Zusammenarbeit",
        "Wie wir arbeiten, damit im Ernstfall niemand erst suchen muss.",
    ),
)


#: The questionnaire, in the order it is asked. Written to be read out loud in a
#: kick-off call: every one of these is a question a PR consultant actually puts
#: to a new client, which is why none of them is phrased as a field label.
QUESTIONS: tuple[Question, ...] = (
    # --- Was das Unternehmen ist ---------------------------------------------
    Question(
        "satz", "unternehmen",
        "Was verkaufen Sie, in einem Satz, ohne Fachbegriffe?",
        "Wenn der Satz eine Erklärung braucht, ist es noch nicht der Satz.",
        InputKind.ABSATZ,
        (Feed(Target.PROFIL, "Geschäftsfeld"), Feed(Target.GUIDE, "Kernbotschaft")),
    ),
    Question(
        "sprecher", "unternehmen",
        "Wer spricht für das Unternehmen, und wozu?",
        "Name, Rolle, und für welche Themen diese Person zitierbar ist.",
        InputKind.LISTE,
        (Feed(Target.PROFIL, "Sprecher"),),
        note="wird in jedem Anschreiben verwendet",
        placeholder="Weitere Person, Rolle, Themen",
    ),
    Question(
        "wettbewerber", "unternehmen",
        "Wen halten Sie für Ihren wichtigsten Wettbewerber, und warum?",
        "Wichtig für den Share of Voice. Die Vergleichsgruppe ist sonst geraten.",
        InputKind.ZEILE,
        (Feed(Target.VERGLEICHSGRUPPE),),
        placeholder="Unternehmen, und in einem Halbsatz warum",
    ),
    Question(
        "zielgruppe", "unternehmen",
        "Wer trifft die Kaufentscheidung, und wen müssen wir dafür erreichen?",
        "Nicht die Branche, sondern die Person, die am Ende unterschreibt.",
        InputKind.ABSATZ,
        (Feed(Target.PROFIL, "Zielgruppe"),),
    ),
    # --- Was gesagt werden darf, und was nie ----------------------------------
    Question(
        "nie_satz", "sagen",
        "Welchen Satz sollen wir über Sie nie schreiben?",
        "Wörtlich, so wie er nicht dastehen soll.",
        InputKind.ABSATZ,
        (Feed(Target.NOGO),),
        note="jeder Text wird dagegen geprüft",
    ),
    Question(
        "schweigen", "sagen",
        "Gibt es ein Thema, zu dem Sie grundsätzlich schweigen?",
        "Laufende Verfahren, Preise, Kundennamen, eine Personalie.",
        InputKind.ABSATZ,
        (Feed(Target.NOGO),),
        placeholder="Thema, und ob Schweigen oder eine Sprachregelung gilt",
    ),
    Question(
        "unwahrheit", "sagen",
        "Was behaupten Ihre Wettbewerber, das schlicht nicht stimmt?",
        "Die ergiebigste Frage im Fragebogen. Hier liegen die Thesen.",
        InputKind.ABSATZ,
        (Feed(Target.THEMENFELDER),),
        note="Material für Impulse",
        placeholder="Behauptung, und womit Sie dagegenhalten können",
    ),
    Question(
        "wortwahl", "sagen",
        "Welche Wörter benutzen Sie über sich selbst, und welche nie?",
        "Heißt es Kunden oder Partner, Mitarbeitende oder Team? Ein falsches "
        "Wort fällt sofort auf.",
        InputKind.ABSATZ,
        (Feed(Target.GUIDE, "Tonalität"),),
    ),
    Question(
        "zahlen", "sagen",
        "Welche Zahlen dürfen genannt werden, und welche nie?",
        "Umsatz, Kundenzahl, Finanzierung. Was nicht raus darf, muss hier stehen.",
        InputKind.ABSATZ,
        (Feed(Target.NOGO),),
    ),
    Question(
        "freigabe", "sagen",
        "Wer gibt einen Text frei, bevor er rausgeht?",
        "Name und Rolle. Und ob das auch für ein einzelnes Zitat gilt.",
        InputKind.ZEILE,
        (Feed(Target.GUIDE, "Freigabe"),),
    ),
    # --- Was erreicht werden soll ---------------------------------------------
    Question(
        "zwoelf_monate", "ziele",
        "Was soll in zwölf Monaten über Sie in der Presse stehen, das heute nicht dasteht?",
        "Ein Satz, den Sie in einem Artikel lesen wollen.",
        InputKind.ABSATZ,
        (Feed(Target.GUIDE, "Zielbild"),),
    ),
    Question(
        "anlaesse", "ziele",
        "Was steht in den nächsten Monaten an, worüber man berichten könnte?",
        "Produkt, Zahlen, Personalie, Standort, Studie — mit ungefährem Datum.",
        InputKind.ABSATZ,
        (Feed(Target.THEMENFELDER),),
    ),
    Question(
        "wirkung", "ziele",
        "Welche Entscheidung soll die Berichterstattung bei Ihren Kunden auslösen?",
        "PR ohne beabsichtigte Wirkung ist Dekoration.",
        InputKind.ABSATZ,
        (Feed(Target.GUIDE, "Kernbotschaft"),),
    ),
    Question(
        "messgroesse", "ziele",
        "Woran würden Sie in einem Jahr sehen, dass sich das gelohnt hat?",
        "Eine Zahl oder ein konkretes Ereignis, kein Gefühl.",
        InputKind.ZEILE,
        (Feed(Target.GUIDE, "Zielbild"),),
    ),
    # --- Medien und Beziehungen ------------------------------------------------
    Question(
        "pflichtmedien", "medien",
        "In welchem Medium müssen Sie vorkommen, damit Ihre Kunden es sehen?",
        "Ein Fachtitel zählt hier mehr als die FAZ, wenn dort eingekauft wird.",
        InputKind.ZEILE,
        (Feed(Target.PROFIL, "Zielmedien"),),
    ),
    Question(
        "journalisten", "medien",
        "Zu welchen Journalistinnen und Journalisten haben Sie schon einen Draht?",
        "Name und Titel reichen. Ein bestehender Kontakt ist mehr wert als eine "
        "kalte Liste.",
        InputKind.LISTE,
        (Feed(Target.KONTAKTE),),
        placeholder="Weiterer Name, Titel",
    ),
    Question(
        "schieflage", "medien",
        "Gab es eine Berichterstattung, die schiefging?",
        "Was passiert ist, und was daraus gilt. Das erklärt eine Empfindlichkeit "
        "besser als jede Regel.",
        InputKind.ABSATZ,
        (Feed(Target.NOGO),),
    ),
    Question(
        "interview", "medien",
        "Wofür stehen Sie für ein Interview zur Verfügung, und wofür nie?",
        "Trennt die gute Anfrage von der, die Ärger macht.",
        InputKind.ABSATZ,
        (Feed(Target.PROFIL, "Sprecher"),),
    ),
    # --- Zusammenarbeit --------------------------------------------------------
    Question(
        "ansprechpartner", "zusammenarbeit",
        "Wer ist bei Ihnen unser erster Ansprechpartner, und wie schnell erreichen wir ihn?",
        "Auch: wer entscheidet, wenn diese Person im Urlaub ist.",
        InputKind.ZEILE,
        (Feed(Target.PROFIL, "Pressekontakt"),),
    ),
    Question(
        "eskalation", "zusammenarbeit",
        "Wen rufen wir an, wenn abends um sieben etwas passiert?",
        "Name und Nummer. Diese Frage wird sonst genau einmal zu spät gestellt.",
        InputKind.ZEILE,
        (Feed(Target.PROFIL, "Krisenkontakt"),),
    ),
)

QUESTIONS_BY_KEY: dict[str, Question] = {q.key: q for q in QUESTIONS}
SECTIONS_BY_KEY: dict[str, Section] = {s.key: s for s in SECTIONS}

#: Which profile column each named profile slot fills. The questionnaire names
#: its targets in words — the page says "Profil · Geschäftsfeld" to a consultant —
#: and this is the one place those words become a key in ``profile.FIELDS``.
#:
#: Two questions may name the same slot (who may be quoted, and on what) and both
#: answers then land in that one field, in the order they are asked. That is the
#: honest result: the profile has one line for spokespeople, and a consultant
#: reading it wants both halves of what the client said.
_PROFILE_SLOTS: dict[str, str] = {
    "Geschäftsfeld": "geschaeftsfeld",
    "Sprecher": "sprecher",
    "Zielgruppe": "zielgruppe",
    "Zielmedien": "zielmedien",
    "Pressekontakt": "pressekontakt",
    "Krisenkontakt": "krisenkontakt",
}

#: What a finished questionnaire looks like, for the progress figure.
TOTAL = len(QUESTIONS)


def _check_question_set() -> None:
    """Fail at import if the question set contradicts itself.

    The fixture test checks the same three things, but a broken set should not
    get as far as rendering a page: a question with no target would silently be a
    form field, and a duplicate key would overwrite a stored answer.
    """
    if len(QUESTIONS_BY_KEY) != len(QUESTIONS):
        raise ValueError("two onboarding questions share a key")
    for question in QUESTIONS:
        if not question.feeds:
            raise ValueError(f"question {question.key!r} declares no target")
        if question.section not in SECTIONS_BY_KEY:
            raise ValueError(f"question {question.key!r} is in no known section")
        for feed in question.feeds:
            # A bare string here would render — ``Target`` is a ``StrEnum`` — and
            # would name a destination that does not exist, which is the one
            # thing the declaration is for.
            if not isinstance(feed, Feed) or not isinstance(feed.target, Target):
                raise ValueError(f"question {question.key!r} feeds no known target")
            # A profile slot with no column behind it is the same failure one
            # level down: the page would promise "Füllt Profil · Sprecher" and
            # the answer would go nowhere.
            if feed.target is Target.PROFIL:
                field = _PROFILE_SLOTS.get(feed.slot)
                if field is None or field not in profile.FIELDS_BY_KEY:
                    raise ValueError(
                        f"question {question.key!r} names the profile slot "
                        f"{feed.slot!r}, which fills no profile field"
                    )


_check_question_set()


def by_section() -> tuple[tuple[Section, tuple[Question, ...]], ...]:
    """The questionnaire grouped for rendering, in the order it is asked."""
    return tuple(
        (section, tuple(q for q in QUESTIONS if q.section == section.key))
        for section in SECTIONS
    )


def entries(value: str) -> list[str]:
    """A list answer, one entry per line, blanks dropped."""
    return [line.strip() for line in value.split(_ENTRY_SEPARATOR) if line.strip()]


def _one_line(entry: str) -> str:
    """One entry, flattened onto the single line the store keeps it on.

    Entries are separated by newlines, so an entry holding one would come back as
    two on the next render: two names pasted from a mail, one under the other,
    would quietly become two spokespeople rather than the one entry somebody
    added. The page's own ``<input type=text>`` cannot produce it; the store is
    where the rule belongs anyway.
    """
    return " ".join(entry.split())


# --- The answer store ----------------------------------------------------------


def answers(session: Session, client_id: int) -> dict[str, OnboardingAnswer]:
    """Everything answered or skipped for this mandate, keyed by question.

    A question with no entry here is unanswered, which is the third state and a
    useful one: it says the foundation is thin exactly there.
    """
    rows = session.scalars(
        select(OnboardingAnswer).where(OnboardingAnswer.client_id == client_id)
    ).all()
    return {row.key: row for row in rows}


def _stored(session: Session, client_id: int, key: str) -> OnboardingAnswer | None:
    return session.scalars(
        select(OnboardingAnswer).where(
            OnboardingAnswer.client_id == client_id, OnboardingAnswer.key == key
        )
    ).first()


def _write(
    session: Session,
    client_id: int,
    key: str,
    *,
    value: str,
    skipped: bool,
    answered_by: str,
) -> OnboardingAnswer:
    """Store one answer row, surviving a concurrent first save of the same question.

    Read-then-insert races here for real. The routes are sync, so FastAPI runs
    them in a threadpool and two requests genuinely interleave: two open tabs, or
    "Speichern" and "Übergehen" clicked in turn — different elements, so htmx's
    per-element queue does not serialise them. Both would find no row, both would
    insert, and the loser would hit ``uq_onboarding_answers_key`` and take a
    sentence transcribed from a call down with it in a 500.

    So the loser rolls back, rereads the row the winner just wrote, and applies
    its own answer on top. Last writer wins, which is what "the answer is stored
    as it is entered" has to mean when two of them arrive at once — and the
    unique constraint keeps doing its job of refusing a second row.
    """

    def apply(row: OnboardingAnswer) -> OnboardingAnswer:
        row.value = value
        row.skipped = skipped
        row.answered_at = dt.datetime.now(dt.UTC)
        row.answered_by = answered_by
        return row

    fresh = _stored(session, client_id, key) or OnboardingAnswer(
        client_id=client_id, key=key
    )
    session.add(apply(fresh))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        won = _stored(session, client_id, key)
        if won is None:
            # Not the unique constraint after all — a missing mandate, say. That
            # is a caller's bug and has to stay visible.
            raise
        session.add(apply(won))
        session.commit()
        return won
    return fresh


def save_answer(
    session: Session,
    client: Client,
    key: str,
    value: str,
    *,
    answered_by: str = ANSWERED_BY_DEFAULT,
) -> OnboardingAnswer | None:
    """Store one answer. Returns ``None`` for an unknown key or an empty answer.

    Answering the same question again replaces what was there and moves the
    timestamp; it never adds a second row, because a mandate has one answer to
    each question and a pile of versions would only raise the question of which
    one counts.

    An empty value deletes the row rather than storing a blank, so clearing an
    answer returns the question to unanswered. That matters: a row holding an
    empty string would keep claiming the question had been dealt with, which is
    the one thing the three states exist to prevent.
    """
    if key not in QUESTIONS_BY_KEY:
        return None
    value = (value or "").strip()
    if not value:
        existing = _stored(session, client.id, key)
        if existing is not None:
            session.delete(existing)
            session.commit()
        return None
    # ``skipped=False``: answering a question that was passed over is the
    # consultant coming back to it, and the skip is spent.
    return _write(
        session, client.id, key, value=value, skipped=False, answered_by=answered_by
    )


def skip(
    session: Session,
    client: Client,
    key: str,
    *,
    answered_by: str = ANSWERED_BY_DEFAULT,
) -> OnboardingAnswer | None:
    """Mark one question as deliberately passed over.

    Stored, not merely left blank: "asked and there is no answer" and "never got
    to it" are different states of the same foundation, and only one of them is a
    reason to go back to the client. Any previous value is dropped, because a
    skipped question showing yesterday's text would be neither.
    """
    if key not in QUESTIONS_BY_KEY:
        return None
    return _write(
        session, client.id, key, value="", skipped=True, answered_by=answered_by
    )


def add_entry(
    session: Session,
    client: Client,
    key: str,
    entry: str,
    *,
    answered_by: str = ANSWERED_BY_DEFAULT,
) -> OnboardingAnswer | None:
    """Append one line to a list answer, leaving the others alone.

    An entry already on the list is a double submit rather than a second person:
    two identical chips cannot be told apart, and the delete button on either one
    removes whichever index it happens to carry.
    """
    question = QUESTIONS_BY_KEY.get(key)
    entry = _one_line(entry)
    if question is None or not question.is_list or not entry:
        return None
    existing = _stored(session, client.id, key)
    lines = entries(existing.value) if existing and not existing.skipped else []
    if any(line.casefold() == entry.casefold() for line in lines):
        return existing
    lines.append(entry)
    return save_answer(
        session, client, key, _ENTRY_SEPARATOR.join(lines), answered_by=answered_by
    )


def remove_entry(
    session: Session,
    client: Client,
    key: str,
    index: int,
    *,
    answered_by: str = ANSWERED_BY_DEFAULT,
) -> OnboardingAnswer | None:
    """Drop one line from a list answer. Removing the last one clears it."""
    question = QUESTIONS_BY_KEY.get(key)
    existing = _stored(session, client.id, key)
    if question is None or not question.is_list or existing is None:
        return None
    lines = entries(existing.value)
    if not 0 <= index < len(lines):
        return None
    del lines[index]
    return save_answer(
        session, client, key, _ENTRY_SEPARATOR.join(lines), answered_by=answered_by
    )


# --- How much of the foundation is actually there ------------------------------


@dataclass(frozen=True, slots=True)
class SectionProgress:
    """One line of the rail: how far this section got."""

    section: Section
    answered: int
    skipped: int
    total: int

    @property
    def settled(self) -> int:
        return self.answered + self.skipped

    @property
    def state(self) -> Progress:
        if self.settled >= self.total:
            return Progress.FERTIG
        return Progress.TEILWEISE if self.settled else Progress.OFFEN

    #: The rail asks for these rather than comparing ``state`` against a string:
    #: the enum is the value set, and a renamed member should break here where the
    #: fixture tests run, not silently draw the wrong tick.
    @property
    def is_done(self) -> bool:
        return self.state is Progress.FERTIG

    @property
    def is_partial(self) -> bool:
        return self.state is Progress.TEILWEISE


@dataclass(frozen=True, slots=True)
class Completeness:
    """The progress figure, and the sentence that goes with it.

    ``settled`` counts answered plus skipped, because a question the client
    declined to answer is dealt with even though it produced nothing. What is
    *not* dealt with is ``remaining``, which the page states in words: a bar
    alone says "some" where a consultant needs "acht".
    """

    answered: int
    skipped: int
    total: int
    last_answered_at: dt.datetime | None
    sections: tuple[SectionProgress, ...]

    @property
    def settled(self) -> int:
        return self.answered + self.skipped

    @property
    def remaining(self) -> int:
        return self.total - self.settled

    @property
    def percent(self) -> int:
        """For the bar. Zero questions is impossible, but division by it is not."""
        return round(100 * self.settled / self.total) if self.total else 0

    @property
    def started(self) -> bool:
        return self.settled > 0


def _is_answered(row: OnboardingAnswer) -> bool:
    return not row.skipped and bool(row.value.strip())


def completeness(
    session: Session,
    client_id: int,
    *,
    stored: dict[str, OnboardingAnswer] | None = None,
) -> Completeness:
    """How much of this mandate's foundation exists, per section and overall.

    A caller that already holds :func:`answers` — every render on the page does —
    passes it in rather than making the same twenty rows come back a second time.
    """
    return _completeness(answers(session, client_id) if stored is None else stored)


def completeness_by_client(
    session: Session, client_ids: list[int]
) -> dict[int, Completeness]:
    """The same figure for a whole portfolio, in one query rather than per row.

    The client list shows it on every card, and twenty mandates asking for their
    own answers separately would be twenty round trips to render one line each.
    A mandate with no questionnaire at all is present in the result with
    everything at zero, because "nothing answered" is a state the list must state
    rather than a row it can leave out.
    """
    grouped: dict[int, dict[str, OnboardingAnswer]] = {cid: {} for cid in client_ids}
    if client_ids:
        rows = session.scalars(
            select(OnboardingAnswer).where(OnboardingAnswer.client_id.in_(client_ids))
        ).all()
        for row in rows:
            grouped.setdefault(row.client_id, {})[row.key] = row
    return {cid: _completeness(stored) for cid, stored in grouped.items()}


def _completeness(stored: dict[str, OnboardingAnswer]) -> Completeness:
    sections = tuple(
        SectionProgress(
            section=section,
            answered=sum(
                1 for q in questions
                if (row := stored.get(q.key)) is not None and _is_answered(row)
            ),
            skipped=sum(
                1 for q in questions
                if (row := stored.get(q.key)) is not None and row.skipped
            ),
            total=len(questions),
        )
        for section, questions in by_section()
    )
    timestamps = [
        row.answered_at for key, row in stored.items() if key in QUESTIONS_BY_KEY
    ]
    return Completeness(
        answered=sum(s.answered for s in sections),
        skipped=sum(s.skipped for s in sections),
        total=TOTAL,
        last_answered_at=max(timestamps) if timestamps else None,
        sections=sections,
    )


# --- Turning answers into offers -----------------------------------------------
#
# Everything below returns; nothing below adopts. The one write is the guide
# source that records where the material came from, which is provenance rather
# than policy: it says the kick-off is a source of this mandate's guide, exactly
# as an uploaded brand book is, and the guide itself stays untouched until a
# person saves one.


def _answered(
    stored: dict[str, OnboardingAnswer], question: Question
) -> OnboardingAnswer | None:
    """The row for this question if it holds an answer; ``None`` otherwise.

    A skipped question is *not* an answer. It is a recorded "we asked and there
    is nothing to get", and adopting it would put an empty rule in a guide.
    """
    row = stored.get(question.key)
    return row if row is not None and _is_answered(row) else None


def _feeding(target: Target) -> tuple[Question, ...]:
    return tuple(q for q in QUESTIONS if any(f.target is target for f in q.feeds))


def to_proposals(
    session: Session,
    client_id: int,
    *,
    stored: dict[str, OnboardingAnswer] | None = None,
) -> list[profile.Proposal]:
    """Answers that map to a profile field, as proposals on that field.

    Proposals, not writes: they arrive on the profile page beside the ones the
    web research produces and are accepted the same way, with one difference the
    page makes visible. A researched value is never allowed over something a
    person typed; a kick-off answer is, because the person who runs the company
    outranks the page written about it — and what it replaces stays legible
    beside it (DEC-2 option A).

    The source is the questionnaire by name and never a URL. Nobody published
    the kick-off call, and a citation that links nowhere would be a worse lie
    than no link at all.
    """
    stored = answers(session, client_id) if stored is None else stored
    by_field: dict[str, list[str]] = {}
    for question in _feeding(Target.PROFIL):
        row = _answered(stored, question)
        if row is None:
            continue
        for feed in question.feeds:
            if feed.target is Target.PROFIL:
                by_field.setdefault(_PROFILE_SLOTS[feed.slot], []).append(
                    row.value.strip()
                )
    return [
        profile.Proposal(
            key=key,
            value="\n".join(values),
            source_title=SOURCE_NAME,
            filled_by=SOURCE_NAME,
            supersedes=True,
        )
        for key, values in by_field.items()
    ]


def to_rivals(
    session: Session,
    client: Client,
    *,
    stored: dict[str, OnboardingAnswer] | None = None,
) -> list[RivalSuggestion]:
    """The competitors the client named, offered for the comparison set.

    Through :func:`newspulse.rivals.from_named`, so a named competitor takes the
    same path as a suggested one and ends in the same click. Nothing is created
    and nothing is linked here: a wrong competitor does not merely look odd, it
    lands in the share-of-voice arithmetic and quietly changes a number the
    agency reports.
    """
    stored = answers(session, client.id) if stored is None else stored
    named = [
        row.value
        for question in _feeding(Target.VERGLEICHSGRUPPE)
        if (row := _answered(stored, question)) is not None
    ]
    return rivals.from_named(client, named)


#: Re-exported so a caller that generates a draft can catch the refusal without
#: importing the guide module for one exception type.
ExtractionError = guide.ExtractionError

#: The heading the client's own no-go sentences sit under in the draft. They are
#: repeated under it verbatim even where the distillation already worked them in,
#: because a rule that has been reworded is a different rule.
_NOGO_HEADING = "No-Gos, wörtlich aus dem Kickoff:"

#: What the draft says instead of inventing a section nobody answered.
_MISSING_HEADING = "Im Kickoff nicht beantwortet:"
_MISSING_NOTE = "Zu diesen Punkten liegt nichts vor. Nichts ergänzen."


@dataclass(frozen=True, slots=True)
class GuideDraft:
    """A proposed guide, and what the questionnaire had to say about it.

    ``text`` is what the consultant edits and may save; nothing here has been
    stored as a guide. ``verbatim`` and ``missing`` are the two things the draft
    must be able to prove: every no-go is in it word for word, and the sections
    nobody answered are named rather than filled in.
    """

    text: str
    verbatim: tuple[str, ...]
    missing: tuple[Section, ...]
    #: The ``GuideSource`` row recording that the kick-off is where this came from.
    source_id: int

    @property
    def has_gaps(self) -> bool:
        return bool(self.missing)


def _source_text(stored: dict[str, OnboardingAnswer]) -> str:
    """The questionnaire as the distillation reads it: question, then answer.

    Unanswered and skipped questions are written out as such rather than left
    out. The prompt forbids inventing what is not in the documents, and a
    question visibly standing there without an answer is the strongest possible
    version of that instruction.
    """
    blocks: list[str] = []
    for section, questions in by_section():
        lines = [f"## {section.title}"]
        for question in questions:
            lines.append(f"F: {question.text}")
            row = stored.get(question.key)
            if row is None:
                lines.append("A: (nicht beantwortet)")
            elif row.skipped:
                lines.append("A: (übergangen — es gibt dazu keine Antwort)")
            else:
                lines.append(f"A: {row.value.strip()}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _nogos(stored: dict[str, OnboardingAnswer]) -> tuple[str, ...]:
    """Every answer that becomes a rule, exactly as it was given."""
    return tuple(
        row.value.strip()
        for question in _feeding(Target.NOGO)
        if (row := _answered(stored, question)) is not None
    )


def _unanswered_sections(stored: dict[str, OnboardingAnswer]) -> tuple[Section, ...]:
    """The sections that produced nothing at all — skipped counts as nothing."""
    return tuple(
        section
        for section, questions in by_section()
        if not any(_answered(stored, q) for q in questions)
    )


def _closing_block(verbatim: tuple[str, ...], missing: tuple[Section, ...]) -> str:
    """The part of the draft that is not the model's to write."""
    lines: list[str] = []
    if verbatim:
        lines.append(_NOGO_HEADING)
        lines.extend(f"· {rule}" for rule in verbatim)
    if missing:
        if lines:
            lines.append("")
        titles = ", ".join(section.title for section in missing)
        lines.append(f"{_MISSING_HEADING} {titles}. {_MISSING_NOTE}")
    return "\n".join(lines)


def _fit(distilled: str, block: str) -> str:
    """The draft inside the guide's character budget, rules first.

    The budget is a real constraint — the guide is prepended to every prompt for
    every mandate every day — and something has to give when both halves are
    long. It is never the client's own sentences: a no-go trimmed mid-clause is
    a rule nobody wrote, which is the failure this whole path exists to avoid.
    So the verbatim block is kept whole and the distilled prose is what shrinks.
    """
    if not block:
        return distilled[: guide.GUIDE_MAX_CHARS].strip()
    budget = guide.GUIDE_MAX_CHARS - len(block) - 2
    if budget <= 0:
        return block
    return f"{distilled[:budget].rstrip()}\n\n{block}"


def to_guide_draft(
    session: Session,
    client: Client,
    *,
    stored: dict[str, OnboardingAnswer] | None = None,
    invoke=None,
) -> GuideDraft:
    """Draft a communications guide from the kick-off answers. Saves no guide.

    Through :func:`newspulse.guide.distill`, which already exists for uploaded
    brand books and already ends in a proposal the consultant edits. Feeding it
    the answers keeps one path to a guide rather than two, and keeps the rule
    that dictated material is never applied directly.

    The answers are filed as a ``GuideSource`` first, both because the
    distillation reads its sources from there and because the guide's provenance
    should say the kick-off is one of them, alongside the documents. That record
    survives a failed distillation, which is right: it describes where the
    material came from, not what came of it.

    Raises :class:`newspulse.guide.ExtractionError` when nothing has been
    answered, so "no answers yet" and "the model failed" stay distinguishable.

    ``invoke`` defaults to the model through a sentinel rather than in the
    signature, because the page calls this with no arguments: bound in the
    signature, the default would be captured at import and a test driving the
    button would shell out to `claude` with no way to stop it.
    """
    invoke = invoke_with_fallback if invoke is None else invoke
    stored = answers(session, client.id) if stored is None else stored
    if not any(_answered(stored, question) for question in QUESTIONS):
        raise ExtractionError(
            "Noch keine Antwort aus dem Kickoff — ohne Antworten gibt es nichts "
            "zu destillieren."
        )
    source = guide.replace_source(session, client, SOURCE_NAME, _source_text(stored))
    distilled = guide.distill(session, client, invoke=invoke)
    verbatim = _nogos(stored)
    missing = _unanswered_sections(stored)
    return GuideDraft(
        text=_fit(distilled, _closing_block(verbatim, missing)),
        verbatim=verbatim,
        missing=missing,
        source_id=source.id,
    )


__all__ = [
    "ANSWERED_BY_DEFAULT",
    "Completeness",
    "ExtractionError",
    "GuideDraft",
    "SOURCE_NAME",
    "Feed",
    "InputKind",
    "Progress",
    "QUESTIONS",
    "QUESTIONS_BY_KEY",
    "Question",
    "SECTIONS",
    "SECTIONS_BY_KEY",
    "Section",
    "SectionProgress",
    "TOTAL",
    "Target",
    "add_entry",
    "answers",
    "by_section",
    "completeness",
    "completeness_by_client",
    "entries",
    "remove_entry",
    "save_answer",
    "skip",
    "to_guide_draft",
    "to_proposals",
    "to_rivals",
]
