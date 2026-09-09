"""Die Herkunft einer Twin-Aussage, und die Regel, die daran hängt.

Das Vokabular stammt wörtlich aus ``RAUTE_OS_INPUT-Master.xlsx`` / ``99_Listen``.
Diese Datei hält zwei Dinge fest, die man später leicht "aufräumt" und dabei
kaputtmacht: dass die Werte genau die der Tabelle sind, und dass alles, was
nicht ``Fact`` ist, nach außen gekennzeichnet gehört.

Der Prüfstein ist Lucas' eigenes Beispiel: „400 % THG-Minderung bei BeyondZero"
ist ein Unternehmensclaim. Wird er wie ein bestätigter Fakt behandelt, zitiert
ihn Ask RAUTE als Tatsache, begründet eine Opportunity ihre Relevanz damit, und
ein Pitch behauptet ihn gegenüber einer Journalistin, die ihn prüft.
"""

from __future__ import annotations

import pytest

from newspulse import evidence


# --- Das Vokabular ist das der Tabelle ---------------------------------------------


def test_the_seven_evidence_types_are_the_sheets_own_words():
    """Nicht sechs, nicht "hübschere". Lucas füllt den Bogen von Hand aus, und
    ein Import, der "Client Statement" nicht kennt, verliert schweigend Zeilen."""
    assert [k.value for k in evidence.EvidenceType] == [
        "Fact",
        "Client Statement",
        "Management Belief",
        "Customer Perception",
        "RAUTE Hypothesis",
        "External Observation",
        "Derived Insight",
    ]


def test_every_axis_carries_the_sheets_values():
    assert [c.value for c in evidence.Confidence] == ["High", "Medium", "Low", "Unknown"]
    assert [c.value for c in evidence.Confidentiality] == [
        "Public", "Client Confidential", "RAUTE Internal", "Highly Restricted",
    ]
    assert [u.value for u in evidence.ExternalUse] == [
        "Allowed", "Approval Required", "Internal Only",
    ]
    assert [r.value for r in evidence.Requirement] == ["Pflicht", "Empfohlen", "Optional"]
    assert len(list(evidence.SourceType)) == 15
    assert len(list(evidence.FactStatus)) == 8


def test_the_three_states_the_quality_check_looks_for_exist():
    """Spezifikation 08 fragt nach veralteten, widersprüchlichen und
    ungeprüften Aussagen. Ohne diese drei Zustände hat sie nichts zu lesen."""
    values = {s.value for s in evidence.FactStatus}

    assert {"Needs Verification", "Outdated", "Contradictory"} <= values


# --- Sieben fein gespeichert, vier grob angezeigt ------------------------------------


def test_the_seven_fold_onto_the_four_classes_of_the_spec():
    """Spezifikation 01 nannte vier, das Blatt hat sieben. Gespeichert wird fein,
    angezeigt grob — eine Warnung über einem fertigen Text muss kurz sein, um
    gelesen zu werden."""
    assert set(evidence.COARSE.values()) == {
        "Bestätigter Fakt",
        "Kundenaussage",
        "External Observation",
        "Unsere Hypothese",
    }
    assert len(evidence.COARSE) == len(list(evidence.EvidenceType))


@pytest.mark.parametrize(
    "fine",
    [
        evidence.EvidenceType.CLIENT_STATEMENT,
        evidence.EvidenceType.MANAGEMENT_BELIEF,
        evidence.EvidenceType.CUSTOMER_PERCEPTION,
    ],
)
def test_the_three_kinds_of_client_voice_read_as_one_outside(fine):
    """Was die Geschäftsführung glaubt und was ein Kunde wahrnimmt, ist
    innerlich verschieden und nach außen dasselbe: eine Aussage des Kunden."""
    assert evidence.coarse(fine) == "Kundenaussage"


def test_an_unknown_value_survives_rather_than_becoming_a_fact():
    """Ein Wert aus einer künftigen Fassung des Blattes. Unbekannt ist nicht
    harmlos — er wird durchgereicht, damit er sichtbar bleibt."""
    assert evidence.coarse("Whitepaper Claim") == "Whitepaper Claim"


# --- Die Regel, die nach außen wirkt ------------------------------------------------


def test_only_a_confirmed_fact_may_go_out_unqualified():
    """Die Regel, die Spezifikation 08 prüft. Alles andere ist die Behauptung
    von jemandem, und wer sie aufstellt, gehört dazugesagt."""
    unqualified = [k for k in evidence.EvidenceType if not evidence.needs_attribution(k)]

    assert unqualified == [evidence.EvidenceType.FACT]


def test_the_beyondzero_claim_needs_attribution():
    """Lucas' eigenes Beispiel, als Test."""
    assert evidence.needs_attribution(evidence.EvidenceType.CLIENT_STATEMENT)


@pytest.mark.parametrize("nothing", [None, "", "   "])
def test_an_unclassified_statement_is_treated_as_a_claim_not_as_a_fact(nothing):
    """Die teure Richtung des Irrtums. Jede Zeile aus der Zeit vor diesen
    Spalten hat ein leeres Feld — und ein leeres Feld darf nicht bedeuten, dass
    ein Satz ungekennzeichnet in einen Pitch darf."""
    assert evidence.needs_attribution(nothing) is True


def test_an_unknown_kind_is_treated_as_a_claim_too():
    assert evidence.needs_attribution("Irgendwas Neues") is True


# --- Die Einstufung der Zeilen, die schon da sind ------------------------------------


def test_the_backfill_classifies_nothing_as_a_confirmed_fact():
    """Die wichtigste Zeile der Migration, und die, die man beim Aufräumen
    kaputtmacht.

    Es gibt heute rund hundert Twin-Aussagen in Produktion. Sie pauschal als
    ``Fact`` einzustufen wäre bequem und würde die ganze Unterscheidung ab dem
    ersten Tag wertlos machen: was ein bestätigter Fakt ist, entscheidet ein
    Mensch beim Durchgehen, nicht das Werkzeug über sich selbst.
    """
    from pathlib import Path

    import newspulse

    sql = (
        Path(newspulse.__file__).resolve().parents[2]
        / "migrations" / "versions" / "0057_fact_evidence.py"
    ).read_text(encoding="utf-8")
    statement = sql[sql.index("UPDATE client_facts") : sql.index("WHERE evidence_type")]

    assert "'External Observation'" in statement
    assert "'Fact'" not in statement, "keine bestehende Zeile darf sich selbst adeln"
    assert "'Needs Verification'" in statement, "alle gehören auf die Durchgehen-Liste"
