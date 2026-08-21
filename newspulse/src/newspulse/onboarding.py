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
comparison set is a separate, deliberate act (ONB-02), because an onboarding that
silently writes a client's own words into a guide is how a wrong sentence becomes
policy.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ANSWERED_BY_DEFAULT, Client, OnboardingAnswer

#: List answers are stored as one text column, one entry per line. A child table
#: for two or three names per mandate would buy nothing that ``splitlines`` does
#: not, and it would put the questionnaire's shape into the schema, where the
#: whole point is that the shape is editable code.
_ENTRY_SEPARATOR = "\n"


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
    ),
    Question(
        "wettbewerber", "unternehmen",
        "Wen halten Sie für Ihren wichtigsten Wettbewerber, und warum?",
        "Wichtig für den Share of Voice. Die Vergleichsgruppe ist sonst geraten.",
        InputKind.ZEILE,
        (Feed(Target.VERGLEICHSGRUPPE),),
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
    ),
    Question(
        "unwahrheit", "sagen",
        "Was behaupten Ihre Wettbewerber, das schlicht nicht stimmt?",
        "Die ergiebigste Frage im Fragebogen. Hier liegen die Thesen.",
        InputKind.ABSATZ,
        (Feed(Target.THEMENFELDER),),
        note="Material für Impulse",
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
    existing = _stored(session, client.id, key)
    value = (value or "").strip()
    if not value:
        if existing is not None:
            session.delete(existing)
            session.commit()
        return None
    row = existing or OnboardingAnswer(client_id=client.id, key=key)
    row.value = value
    row.answered_at = dt.datetime.now(dt.UTC)
    row.answered_by = answered_by
    # Answering a question that was passed over is the consultant coming back to
    # it; the skip is spent.
    row.skipped = False
    session.add(row)
    session.commit()
    return row


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
    row = _stored(session, client.id, key) or OnboardingAnswer(
        client_id=client.id, key=key
    )
    row.value = ""
    row.skipped = True
    row.answered_at = dt.datetime.now(dt.UTC)
    row.answered_by = answered_by
    session.add(row)
    session.commit()
    return row


def add_entry(
    session: Session,
    client: Client,
    key: str,
    entry: str,
    *,
    answered_by: str = ANSWERED_BY_DEFAULT,
) -> OnboardingAnswer | None:
    """Append one line to a list answer, leaving the others alone."""
    question = QUESTIONS_BY_KEY.get(key)
    if question is None or not question.is_list or not entry.strip():
        return None
    existing = _stored(session, client.id, key)
    lines = entries(existing.value) if existing and not existing.skipped else []
    lines.append(entry.strip())
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


def completeness(session: Session, client_id: int) -> Completeness:
    """How much of this mandate's foundation exists, per section and overall."""
    stored = answers(session, client_id)
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


__all__ = [
    "ANSWERED_BY_DEFAULT",
    "Completeness",
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
    "entries",
    "remove_entry",
    "save_answer",
    "skip",
]
