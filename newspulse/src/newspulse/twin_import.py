"""Den ausgefüllten RAUTE-Bogen lesen und in den Twin schreiben.

``RAUTE_OS_INPUT-Master.xlsx`` hat 339 Zeilen auf vier Blättern, jede mit ihrer
eigenen Herkunft in acht Spalten daneben. Lucas füllt ihn in Excel aus — mit
Marc und Alex, über Wochen —, und dieser Weg bringt das Ergebnis in einem Zug
herein, statt dass jemand dreihundert Felder abtippt.

Drei Eigenschaften bestimmen den Bau:

* **Gelesen wird, was dasteht, nicht was daraus folgt.** Die Herkunftsspalten
  der Tabelle wandern unverändert in die Zeile: ``Client Statement`` bleibt
  ``Client Statement``. Nichts wird hier zu einem Fakt befördert, weil eine
  Antwort ausführlich klingt.
* **Leere Zeilen sind keine Antworten.** Der Bogen wird über Wochen gefüllt und
  zwischendurch hochgeladen. Eine Zeile ohne Antwort ist eine offene Frage, und
  sie darf einen früher eingetragenen Wert nicht löschen.
* **Erst lesen, dann schreiben.** :func:`read` fasst die Datei nur zusammen —
  wie viele Antworten, wie viele Blätter, was nicht gelesen werden konnte. Das
  Schreiben ist ein zweiter, ausdrücklicher Aufruf, wie beim Mandanten-Import:
  eine Tabelle mit dreihundert Zeilen soll man ansehen, bevor sie einen Twin
  überschreibt.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import IO

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException
from sqlalchemy.orm import Session

from . import profile as profiles
from .models import Client, ClientFact

_log = logging.getLogger(__name__)


class SheetError(Exception):
    """Die Datei ist keine lesbare Fassung des Bogens."""


#: Die Blätter mit Fragen, in der Reihenfolge, in der sie gelesen werden. Das
#: Blatt ``04_External Reality`` fehlt bewusst: es enthält keine Fragen an den
#: Kunden, sondern Rechercheaufträge ("Relevante Wettbewerber identifizieren"),
#: und die erledigt der tägliche Lauf. Sie als Twin-Aussagen zu speichern hiesse,
#: eine Aufgabe für eine Tatsache zu halten.
SHEETS: tuple[str, ...] = (
    "01_Client Facts",
    "02_Client Perspective",
    "03_RAUTE Perspective",
)

#: Spaltenüberschrift → Feld auf der Zeile. Nach Namen, nicht nach Position:
#: der Bogen wächst, und eine eingeschobene Spalte darf nicht dazu führen, dass
#: die Vertraulichkeit im Confidence-Feld landet.
_COLUMNS: dict[str, str] = {
    "ID": "sheet_id",
    "Bereich": "area",
    "Unterbereich": "subarea",
    "Frage / Feld": "question",
    "Antwort / Input": "answer",
    "Pflichtgrad": "requirement",
    "Input Owner": "input_owner",
    "Quelle / Dokument": "source",
    "Source Type": "source_type",
    "Evidence Type": "evidence_type",
    "Datum": "as_of",
    "Confidence": "confidence",
    "Vertraulichkeit": "confidentiality",
    "Externe Nutzung": "external_use",
    "Status": "fact_status",
    "Notizen": "notes",
}

#: Ohne diese Spalten ist es nicht der Bogen, sondern irgendeine Tabelle.
_REQUIRED = ("ID", "Frage / Feld", "Antwort / Input")

#: Wer die Zeilen geschrieben hat, in ``ClientFact.filled_by``. Ein eigener Wert
#: neben ``mensch`` und dem Modellnamen: der Bogen ist von Hand ausgefüllt, aber
#: nicht in diesem Werkzeug, und die Twin-Seite soll das sagen können.
FILLED_BY = "fragebogen"


@dataclass(frozen=True, slots=True)
class Row:
    """Eine ausgefüllte Zeile des Bogens."""

    sheet: str
    sheet_id: str
    question: str
    answer: str
    area: str = ""
    subarea: str = ""
    requirement: str = ""
    input_owner: str = ""
    source: str = ""
    source_type: str = ""
    evidence_type: str = ""
    confidence: str = ""
    confidentiality: str = ""
    external_use: str = ""
    fact_status: str = ""
    notes: str = ""
    as_of: dt.datetime | None = None


@dataclass(frozen=True, slots=True)
class Reading:
    """Was in der Datei steht, bevor irgendetwas geschrieben wird."""

    rows: list[Row] = field(default_factory=list)
    #: Zeilen mit einer Frage, aber ohne Antwort. Die Zahl gehört auf den
    #: Bildschirm: "48 von 339 beantwortet" ist die eigentliche Auskunft eines
    #: halb ausgefüllten Bogens.
    unanswered: int = 0
    sheets_read: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def answered(self) -> int:
        return len(self.rows)

    @property
    def total(self) -> int:
        return self.answered + self.unanswered


def _text(value: object) -> str:
    """Eine Zelle als getrimmter Text. ``None`` und Zahlen inbegriffen."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    return str(value).strip()


def _when(value: object) -> dt.datetime | None:
    """Die Datumsspalte als tz-bewusste UTC-Zeit, oder ``None``.

    Excel liefert je nach Formatierung ein ``datetime`` oder einen String. Was
    sich nicht lesen lässt, wird ``None`` statt heute: ein erfundenes Stand-Datum
    ist schlimmer als gar keins — es lässt eine alte Angabe frisch aussehen.
    """
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time(12, 0), tzinfo=dt.UTC)
    text = _text(value)
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def read(source: IO[bytes] | str) -> Reading:
    """Die Datei lesen und zusammenfassen. Schreibt nichts.

    Ein Blatt, das fehlt oder dessen Kopf nicht passt, wird vermerkt und
    übersprungen — der Bogen wird von Hand bearbeitet, und ein umbenanntes Blatt
    darf nicht den ganzen Import verweigern, wenn die anderen drei lesbar sind.
    """
    try:
        book = load_workbook(source, data_only=True, read_only=True)
    except InvalidFileException as exc:
        raise SheetError(f"Keine lesbare .xlsx-Datei: {exc}") from None
    except Exception as exc:  # noqa: BLE001 — jede Lesefehlerform wird zu einer
        raise SheetError(f"Die Datei liess sich nicht öffnen: {exc}") from None

    rows: list[Row] = []
    unanswered = 0
    read_sheets: list[str] = []
    problems: list[str] = []

    for name in SHEETS:
        if name not in book.sheetnames:
            problems.append(f"Blatt {name!r} fehlt")
            continue
        sheet = book[name]
        lines = sheet.iter_rows(values_only=True)
        header = [_text(c) for c in next(lines, ())]
        missing = [c for c in _REQUIRED if c not in header]
        if missing:
            problems.append(f"Blatt {name!r}: Spalte(n) {', '.join(missing)} fehlen")
            continue
        index = {head: i for i, head in enumerate(header) if head in _COLUMNS}
        read_sheets.append(name)

        for line in lines:
            if not line:
                continue

            def cell(head: str, _line=line, _index=index) -> object:
                position = _index.get(head)
                if position is None or position >= len(_line):
                    return None
                return _line[position]

            question = _text(cell("Frage / Feld"))
            if not question:
                continue  # Zwischenüberschrift oder Leerzeile
            answer = _text(cell("Antwort / Input"))
            if not answer:
                unanswered += 1
                continue
            rows.append(
                Row(
                    sheet=name,
                    sheet_id=_text(cell("ID")),
                    question=question,
                    answer=answer,
                    area=_text(cell("Bereich")),
                    subarea=_text(cell("Unterbereich")),
                    requirement=_text(cell("Pflichtgrad")),
                    input_owner=_text(cell("Input Owner")),
                    source=_text(cell("Quelle / Dokument")),
                    source_type=_text(cell("Source Type")),
                    evidence_type=_text(cell("Evidence Type")),
                    confidence=_text(cell("Confidence")),
                    confidentiality=_text(cell("Vertraulichkeit")),
                    external_use=_text(cell("Externe Nutzung")),
                    fact_status=_text(cell("Status")),
                    notes=_text(cell("Notizen")),
                    as_of=_when(cell("Datum")),
                )
            )
    book.close()

    if not read_sheets:
        raise SheetError(
            "Keines der erwarteten Blätter war lesbar: " + "; ".join(problems)
        )
    return Reading(
        rows=rows, unanswered=unanswered, sheets_read=read_sheets, problems=problems
    )


def apply(session: Session, client: Client, reading: Reading) -> int:
    """Die gelesenen Zeilen in den Twin schreiben. Gibt die Anzahl zurück.

    Der Schlüssel ist die ID des Bogens (``A02.01``). Damit trifft ein zweiter
    Upload dieselbe Zeile statt eine zweite anzulegen — und der Bogen wird über
    Wochen gefüllt, also ist der zweite Upload der Normalfall.

    Ein von Hand im Werkzeug eingetragener Wert wird nicht überschrieben,
    sondern verdrängt (``supersede``): beide bleiben sichtbar, bis ein Mensch
    entscheidet. Das ist dieselbe Regel, die eine Kickoff-Antwort gegen die
    Web-Recherche schützt, und sie gilt hier in dieselbe Richtung — der Bogen
    ist zwar von Hand ausgefüllt, aber nicht in diesem Werkzeug, und was jemand
    hier zuletzt korrigiert hat, kennt er besser als eine Tabelle von vorletzter
    Woche.
    """
    existing = profiles.stored(session, client.id)
    written = 0
    for row in reading.rows:
        key = row.sheet_id or row.question[:60]
        current = existing.get(key)
        by_hand = current is not None and current.filled_by == profiles.BY_HAND
        fact = profiles.record(
            session,
            client,
            key,
            row.answer,
            source_title=row.source,
            filled_by=FILLED_BY,
            supersede=by_hand,
        )
        if fact is None:
            continue
        # Die Herkunft, unverändert aus der Tabelle. Kein Wert wird hier
        # hergeleitet: was der Bogen nicht sagt, bleibt leer, und leer gilt als
        # kennzeichnungspflichtig (newspulse.evidence.needs_attribution).
        fact.evidence_type = row.evidence_type or None
        fact.source_type = row.source_type or None
        fact.confidence = row.confidence or None
        fact.confidentiality = row.confidentiality or None
        fact.external_use = row.external_use or None
        fact.fact_status = row.fact_status or None
        fact.input_owner = row.input_owner or None
        fact.as_of = row.as_of
        written += 1
    session.commit()
    _log.info(
        "Fragebogen für %r übernommen: %d Antworten aus %d Blättern",
        client.name,
        written,
        len(reading.sheets_read),
    )
    return written
