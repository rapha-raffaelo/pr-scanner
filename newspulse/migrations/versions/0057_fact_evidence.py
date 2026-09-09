"""jede Twin-Aussage sagt, was für eine Art von Aussage sie ist

Aus ``RAUTE_OS_INPUT-Master.xlsx`` / ``99_Listen``. Acht Spalten, mit dem
Vokabular der Tabelle statt einer hübscheren Übersetzung davon: der Bogen wird
von Hand ausgefüllt, und ein Import, der "Client Statement" nicht kennt, weil
hier jemand "Kundenaussage" schöner fand, verliert schweigend Zeilen.

Warum das vor allem anderen steht: "400 % THG-Minderung bei BeyondZero" ist ein
Unternehmensclaim. Ohne diese Spalten steht er im Twin wie ein bestätigter
Fakt — und dann zitiert ihn Ask RAUTE als Tatsache, begründet eine Opportunity
ihre Relevanz damit, und ein Pitch behauptet ihn gegenüber einer Journalistin,
die ihn prüft.

**Die bestehenden Zeilen werden eingestuft, nicht geraten.** Zwei Fälle, und
für beide gibt es eine belastbare Antwort in den Daten, die schon da sind:

* ``filled_by = 'mensch'`` — ein Berater hat es getippt. Das ist eine bewusste
  Eintragung durch RAUTE, also ``RAUTE Analysis`` als Quelle und
  ``External Observation`` als Art. Nicht ``Fact``: dass ein Mensch es
  eingetragen hat, heißt nicht, dass es unabhängig belegt ist, und die teure
  Richtung des Irrtums ist die andere.
* alles übrige stammt aus der Web-Recherche und bekommt ``AI Research`` und
  ebenfalls ``External Observation``.

Keine Zeile wird zu ``Fact``. Was ein bestätigter Fakt ist, entscheidet ein
Mensch beim Durchgehen — das Werkzeug darf sich diese Einstufung nicht selbst
geben, sonst ist die ganze Unterscheidung ab dem ersten Tag wertlos.

``confidence`` bleibt für alle ``Unknown`` und ``fact_status`` auf
``Needs Verification``: beides ist wahr und beides ist das, was die Twin-Seite
danach zum Durchgehen auflistet.

Revision ID: 0057_fact_evidence
Revises: 0056_desk_actions
Create Date: 2026-09-09
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0057_fact_evidence"
down_revision: str | None = "0056_desk_actions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("evidence_type", 40),
    ("source_type", 40),
    ("confidence", 16),
    ("confidentiality", 32),
    ("external_use", 24),
    ("fact_status", 32),
    ("input_owner", 32),
)


def upgrade() -> None:
    with op.batch_alter_table("client_facts") as batch:
        for name, width in _COLUMNS:
            batch.add_column(sa.Column(name, sa.String(length=width), nullable=True))
        batch.add_column(sa.Column("as_of", sa.DateTime(timezone=True), nullable=True))

    # Die bestehenden Zeilen einstufen. Nach ``filled_by``, weil das die einzige
    # Angabe ist, die heute schon Mensch von Maschine trennt.
    op.execute(
        sa.text(
            "UPDATE client_facts SET "
            "  evidence_type = 'External Observation', "
            "  source_type = CASE WHEN filled_by = 'mensch' "
            "                     THEN 'RAUTE Analysis' ELSE 'AI Research' END, "
            "  input_owner = CASE WHEN filled_by = 'mensch' "
            "                     THEN 'RAUTE' ELSE 'OS / Research' END, "
            "  confidence = 'Unknown', "
            "  fact_status = 'Needs Verification' "
            "WHERE evidence_type IS NULL"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("client_facts") as batch:
        batch.drop_column("as_of")
        for name, _ in reversed(_COLUMNS):
            batch.drop_column(name)
