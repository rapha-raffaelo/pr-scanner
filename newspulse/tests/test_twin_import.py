"""Den ausgefüllten RAUTE-Bogen einlesen.

``RAUTE_OS_INPUT-Master.xlsx`` hat 339 Zeilen auf vier Blättern, jede mit ihrer
Herkunft daneben. Lucas füllt ihn über Wochen mit Marc und Alex aus und lädt ihn
zwischendurch hoch — beides prägt diese Tests: ein halb ausgefüllter Bogen ist
der Normalfall, und der zweite Upload auch.

Die Zeile, die dieser ganze Weg schützt, steht im letzten Abschnitt: was der
Bogen als ``Client Statement`` ausweist, kommt als ``Client Statement`` an. Nichts
wird unterwegs zu einem Fakt befördert, weil es ausführlich klingt.
"""

from __future__ import annotations

import datetime as dt
import io

import pytest
from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import evidence, profile as profiles, twin_import
from newspulse.models import Base, Client

_HEAD = [
    "ID", "Bereich", "Unterbereich", "Frage / Feld", "Antwort / Input",
    "Pflichtgrad", "Input Owner", "Quelle / Dokument", "Source Type",
    "Evidence Type", "Datum", "Confidence", "Vertraulichkeit",
    "Externe Nutzung", "Status", "Notizen",
]


def _book(rows_by_sheet: dict[str, list[list]], *, head=None) -> io.BytesIO:
    """Eine Arbeitsmappe in der Form des Bogens, im Speicher."""
    book = Workbook()
    book.remove(book.active)
    for name, rows in rows_by_sheet.items():
        sheet = book.create_sheet(name)
        sheet.append(head if head is not None else _HEAD)
        for row in rows:
            sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    buffer.seek(0)
    return buffer


def _row(sheet_id, question, answer, **over):
    values = {
        "Pflichtgrad": "Pflicht", "Input Owner": "Kunde / Management",
        "Quelle / Dokument": "", "Source Type": "Client Interview",
        "Evidence Type": "Client Statement", "Datum": None,
        "Confidence": "Medium", "Vertraulichkeit": "Public",
        "Externe Nutzung": "Approval Required", "Status": "Vollständig",
        "Notizen": "",
    }
    values.update(over)
    return [sheet_id, "A. Client Facts", "", question, answer] + [
        values[h] for h in _HEAD[5:]
    ]


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def session(factory):
    with factory() as open_session:
        yield open_session


@pytest.fixture
def client(session):
    row = Client(name="Carbon Farming", aliases=[], keywords=[], alert_topics=[])
    session.add(row)
    session.commit()
    return row


# --- Lesen ---------------------------------------------------------------------------


def test_a_filled_row_arrives_with_every_column_it_carried():
    """Sechzehn Spalten, und die acht rechts davon sind der ganze Punkt."""
    book = _book({"01_Client Facts": [
        _row("A02.01", "Was verkauft das Unternehmen?", "BioLNG aus Reststoffen.",
             **{"Datum": dt.datetime(2026, 8, 1)}),
    ]})

    reading = twin_import.read(book)

    assert reading.answered == 1
    row = reading.rows[0]
    assert row.sheet_id == "A02.01"
    assert row.answer == "BioLNG aus Reststoffen."
    assert row.evidence_type == "Client Statement"
    assert row.confidence == "Medium"
    assert row.confidentiality == "Public"
    assert row.external_use == "Approval Required"
    assert row.as_of == dt.datetime(2026, 8, 1, tzinfo=dt.UTC)


def test_an_unanswered_row_is_counted_not_imported():
    """Der Bogen wird über Wochen gefüllt. "48 von 339 beantwortet" ist die
    eigentliche Auskunft eines halb ausgefüllten Bogens — und eine offene Frage
    darf keinen früher eingetragenen Wert löschen."""
    book = _book({"01_Client Facts": [
        _row("A02.01", "Beantwortet", "Eine Antwort."),
        _row("A02.02", "Noch offen", ""),
        _row("A02.03", "Auch offen", None),
    ]})

    reading = twin_import.read(book)

    assert (reading.answered, reading.unanswered, reading.total) == (1, 2, 3)


def test_a_row_without_a_question_is_neither_answer_nor_gap():
    """Zwischenüberschriften und Leerzeilen. Als offene Frage gezählt würden sie
    die Vollständigkeit dauerhaft drücken."""
    book = _book({"01_Client Facts": [
        _row("A02.01", "Echte Frage", "Antwort."),
        ["", "A. Client Facts", "", "", "", "", "", "", "", "", None, "", "", "", "", ""],
    ]})

    reading = twin_import.read(book)

    assert reading.total == 1


def test_the_research_sheet_is_not_read():
    """``04_External Reality`` enthält keine Fragen an den Kunden, sondern
    Rechercheaufträge — "Relevante Wettbewerber identifizieren". Die erledigt der
    tägliche Lauf; sie als Twin-Aussagen zu speichern hiesse, eine Aufgabe für
    eine Tatsache zu halten."""
    book = _book({
        "01_Client Facts": [_row("A02.01", "Frage", "Antwort.")],
        "04_External Reality": [_row("D02.01", "Wettbewerber identifizieren", "erledigt")],
    })

    reading = twin_import.read(book)

    assert reading.answered == 1
    assert "04_External Reality" not in reading.sheets_read


def test_an_unreadable_date_becomes_no_date_rather_than_today():
    """Ein erfundenes Stand-Datum ist schlimmer als gar keins: es lässt eine
    Angabe von 2023 frisch aussehen."""
    book = _book({"01_Client Facts": [
        _row("A02.01", "Frage", "Antwort.", **{"Datum": "irgendwann letztes Jahr"}),
    ]})

    assert twin_import.read(book).rows[0].as_of is None


def test_a_missing_sheet_is_noted_and_the_others_are_still_read():
    """Der Bogen wird von Hand bearbeitet. Ein umbenanntes Blatt darf nicht den
    ganzen Import verweigern, wenn die anderen lesbar sind."""
    book = _book({"01_Client Facts": [_row("A02.01", "Frage", "Antwort.")]})

    reading = twin_import.read(book)

    assert reading.answered == 1
    assert any("02_Client Perspective" in p for p in reading.problems)


def test_a_sheet_without_the_answer_column_is_refused_by_name():
    book = _book(
        {"01_Client Facts": [["A02.01", "Frage"]]},
        head=["ID", "Frage / Feld"],
    )

    reading_problems = twin_import.read(
        _book({"01_Client Facts": [_row("A", "F", "A")],
               "02_Client Perspective": [["B01.01", "Frage"]]},)
    ).problems
    assert isinstance(reading_problems, list)

    with pytest.raises(twin_import.SheetError, match="Antwort"):
        twin_import.read(book)


def test_something_that_is_not_a_workbook_says_so():
    with pytest.raises(twin_import.SheetError):
        twin_import.read(io.BytesIO(b"das ist keine Tabelle"))


# --- Schreiben -----------------------------------------------------------------------


def test_the_provenance_survives_into_the_twin(session, client):
    """Die Zeile, die dieser ganze Weg schützt."""
    reading = twin_import.read(_book({"01_Client Facts": [
        _row("A08.06", "Welche Aussage können Sie nicht belegen?",
             "400 % THG-Minderung gegenüber fossilem LNG."),
    ]}))

    twin_import.apply(session, client, reading)

    fact = profiles.stored(session, client.id)["A08.06"]
    assert fact.value.startswith("400 %")
    assert fact.evidence_type == "Client Statement"
    assert evidence.needs_attribution(fact.evidence_type), "kein Fakt, eine Behauptung"
    assert fact.confidence == "Medium"


def test_a_second_upload_updates_the_same_row(session, client):
    """Der Bogen wird über Wochen gefüllt, also ist der zweite Upload der
    Normalfall — und er darf die Antwort nicht verdoppeln."""
    first = twin_import.read(_book({"01_Client Facts": [
        _row("A02.01", "Frage", "Erste Antwort."),
    ]}))
    twin_import.apply(session, client, first)
    second = twin_import.read(_book({"01_Client Facts": [
        _row("A02.01", "Frage", "Korrigierte Antwort."),
    ]}))

    twin_import.apply(session, client, second)

    facts = profiles.stored(session, client.id)
    assert len(facts) == 1
    assert facts["A02.01"].value == "Korrigierte Antwort."


def test_a_value_typed_in_the_tool_is_displaced_not_overwritten(session, client):
    """Was jemand hier zuletzt korrigiert hat, kennt er besser als eine Tabelle
    von vorletzter Woche. Beide bleiben sichtbar, bis ein Mensch entscheidet."""
    # Über record, weil eine Bogen-ID kein benanntes Profilfeld ist — das ist
    # dieselbe Tür, die die Twin-Ansicht später benutzt.
    profiles.record(session, client, "A02.01", "Von Hand im Werkzeug.")
    reading = twin_import.read(_book({"01_Client Facts": [
        _row("A02.01", "Frage", "Aus der Tabelle."),
    ]}))

    twin_import.apply(session, client, reading)

    fact = profiles.stored(session, client.id)["A02.01"]
    assert fact.value == "Aus der Tabelle."
    assert fact.superseded_value == "Von Hand im Werkzeug.", "die Handeingabe bleibt"
    assert fact.is_disputed


def test_what_the_sheet_does_not_say_is_not_invented(session, client):
    """Eine Zeile ohne Herkunft bekommt keine. Leer gilt in der Qualitätsprüfung
    als kennzeichnungspflichtig — das ist die sichere Richtung."""
    reading = twin_import.read(_book({"01_Client Facts": [
        _row("A02.01", "Frage", "Antwort.", **{
            "Evidence Type": "", "Confidence": "", "Vertraulichkeit": "",
        }),
    ]}))

    twin_import.apply(session, client, reading)

    fact = profiles.stored(session, client.id)["A02.01"]
    assert fact.evidence_type is None
    assert fact.confidence is None
    assert evidence.needs_attribution(fact.evidence_type)


def test_the_rows_say_they_came_from_the_questionnaire(session, client):
    """Ein eigener Wert neben "mensch" und dem Modellnamen: von Hand ausgefüllt,
    aber nicht in diesem Werkzeug."""
    reading = twin_import.read(_book({"01_Client Facts": [
        _row("A02.01", "Frage", "Antwort."),
    ]}))

    twin_import.apply(session, client, reading)

    assert profiles.stored(session, client.id)["A02.01"].filled_by == "fragebogen"
