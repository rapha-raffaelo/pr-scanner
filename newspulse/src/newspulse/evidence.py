"""Die Herkunft einer Aussage über einen Mandanten, in RAUTEs eigenem Vokabular.

Aus ``RAUTE_OS_INPUT-Master.xlsx``, Blatt ``99_Listen``. Die Werte sind
absichtlich genau die aus der Tabelle und nicht eine hübschere Auswahl davon:
Lucas füllt den Bogen von Hand aus, und ein Import, der „Client Statement"
nicht kennt, weil jemand hier „Kundenaussage" schöner fand, ist ein Import, der
schweigend Zeilen verliert.

Warum das kein Beiwerk ist, sondern das Fundament:

    „400 % THG-Minderung bei BeyondZero"

Das ist ein Unternehmensclaim. Steht es im Twin wie ein bestätigter Fakt, dann
zitiert Ask RAUTE es als Tatsache, begründet eine Opportunity ihre Relevanz
damit, und ein Pitch behauptet es gegenüber einer Journalistin, die es prüft.
Drei Spezifikationen (03, 04, 08) hängen an dieser einen Unterscheidung.

**Sieben Klassen, nicht vier.** Spezifikation 01 nannte vier — bestätigter Fakt,
Kundenaussage, External Observation, unsere Hypothese. Das Blatt hat sieben, und
die drei zusätzlichen sind keine Spitzfindigkeit: die Überzeugung eines
Geschäftsführers, die Wahrnehmung eines Kunden und ein von uns abgeleiteter
Schluss verhalten sich nach außen verschieden. Gespeichert wird deshalb fein,
angezeigt wird grob — :func:`coarse` faltet die sieben auf die vier der
Spezifikation, wo eine Oberfläche knapp sein muss.
"""

from __future__ import annotations

from enum import StrEnum


class EvidenceType(StrEnum):
    """Was für eine Art von Aussage das ist. Die wichtigste der sieben Achsen."""

    FACT = "Fact"
    CLIENT_STATEMENT = "Client Statement"
    MANAGEMENT_BELIEF = "Management Belief"
    CUSTOMER_PERCEPTION = "Customer Perception"
    RAUTE_HYPOTHESIS = "RAUTE Hypothesis"
    EXTERNAL_OBSERVATION = "External Observation"
    DERIVED_INSIGHT = "Derived Insight"


class Confidence(StrEnum):
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"
    UNKNOWN = "Unknown"


class Confidentiality(StrEnum):
    PUBLIC = "Public"
    CLIENT_CONFIDENTIAL = "Client Confidential"
    RAUTE_INTERNAL = "RAUTE Internal"
    HIGHLY_RESTRICTED = "Highly Restricted"


class ExternalUse(StrEnum):
    """Ob eine Aussage nach außen darf. Getrennt von der Vertraulichkeit, weil
    die beiden nicht dasselbe sind: eine öffentliche Zahl kann dennoch der
    Freigabe des Kunden bedürfen, und eine vertrauliche Einschätzung ist auch
    dann nicht extern verwendbar, wenn niemand widerspricht."""

    ALLOWED = "Allowed"
    APPROVAL_REQUIRED = "Approval Required"
    INTERNAL_ONLY = "Internal Only"


class FactStatus(StrEnum):
    """Wo die Aussage in der Pflege steht.

    ``NEEDS_VERIFICATION``, ``OUTDATED`` und ``CONTRADICTORY`` sind die drei,
    auf die die Qualitätsprüfung aus Spezifikation 08 sieht — sie sind der
    Grund, warum ein fertiger Text eine Warnung bekommt.
    """

    OPEN = "Offen"
    IN_PROGRESS = "In Bearbeitung"
    COMPLETE = "Vollständig"
    NEEDS_VERIFICATION = "Needs Verification"
    OUTDATED = "Outdated"
    CONTRADICTORY = "Contradictory"
    REJECTED = "Rejected"
    ARCHIVED = "Archived"


class Requirement(StrEnum):
    """Pflichtgrad. Trägt die Vollständigkeitsrechnung: ein fehlendes
    Optionalfeld ist keine Lücke, ein fehlendes Pflichtfeld ist eine."""

    REQUIRED = "Pflicht"
    RECOMMENDED = "Empfohlen"
    OPTIONAL = "Optional"


class InputOwner(StrEnum):
    """Wer die Aussage beizubringen hat. Nicht dasselbe wie die Quelle: der
    Owner ist eine Zuständigkeit, die Quelle ist eine Herkunft."""

    CLIENT_RAUTE = "Kunde / RAUTE"
    CLIENT_MANAGEMENT = "Kunde / Management"
    RAUTE = "RAUTE"
    OS_RESEARCH = "OS / Research"


class SourceType(StrEnum):
    """Woher die Aussage kommt. Fünfzehn Werte, wörtlich aus dem Blatt."""

    CLIENT_INTERVIEW = "Client Interview"
    MANAGEMENT_INTERVIEW = "Management Interview"
    EMPLOYEE_INTERVIEW = "Employee Interview"
    CUSTOMER_INTERVIEW = "Customer Interview"
    SALES_DATA = "Sales Data"
    CRM = "CRM"
    INTERNAL_DOCUMENT = "Internal Document"
    WEBSITE = "Website"
    MEDIA = "Media"
    EXTERNAL_RESEARCH = "External Research"
    RAUTE_ANALYSIS = "RAUTE Analysis"
    AI_RESEARCH = "AI Research"
    STUDY = "Study"
    PUBLIC_DATABASE = "Public Database"
    OTHER = "Other"


#: Die vier groben Klassen aus Spezifikation 01, auf die die sieben fallen.
#: Für Oberflächen, die keinen Platz für sieben haben — und für die Warnung
#: über einem fertigen Text, die kurz sein muss, um gelesen zu werden.
COARSE: dict[EvidenceType, str] = {
    EvidenceType.FACT: "Bestätigter Fakt",
    EvidenceType.CLIENT_STATEMENT: "Kundenaussage",
    EvidenceType.MANAGEMENT_BELIEF: "Kundenaussage",
    EvidenceType.CUSTOMER_PERCEPTION: "Kundenaussage",
    EvidenceType.EXTERNAL_OBSERVATION: "External Observation",
    EvidenceType.RAUTE_HYPOTHESIS: "Unsere Hypothese",
    EvidenceType.DERIVED_INSIGHT: "Unsere Hypothese",
}

#: Was ohne Kennzeichnung nach außen darf. Genau eine der sieben Klassen: alles
#: andere ist eine Behauptung von jemandem, und wer sie aufstellt, gehört
#: dazugesagt. Das ist die Regel, die Spezifikation 08 prüft.
UNQUALIFIED_OUTSIDE = frozenset({EvidenceType.FACT})


def coarse(kind: EvidenceType | str | None) -> str:
    """Die grobe Klasse zu einer feinen, oder ein leerer String für nichts."""
    if kind is None:
        return ""
    try:
        return COARSE[EvidenceType(kind)]
    except ValueError:
        # Ein Wert, den das Blatt nicht kennt — etwa aus einer künftigen
        # Fassung. Unbekannt ist nicht gleich harmlos: er wird durchgereicht,
        # damit er sichtbar bleibt, statt still zu einem Fakt zu werden.
        return str(kind)


def needs_attribution(kind: EvidenceType | str | None) -> bool:
    """Ob eine Aussage dieser Art nach außen gekennzeichnet werden muss.

    Die Vorgabe für alles Unbekannte ist ``True``. Eine Aussage, deren Herkunft
    das Werkzeug nicht kennt, ist keine, die es als Tatsache ausgeben darf —
    und ein leeres Feld ist genau so ein Fall. Die teure Richtung des Irrtums
    ist die andere.
    """
    if not kind:
        return True
    try:
        return EvidenceType(kind) not in UNQUALIFIED_OUTSIDE
    except ValueError:
        return True
