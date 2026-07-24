"""Unit tests for client import and the CRUD service.

The import tests run against a committed fixture sheet
(``fixtures/clients_sample.xlsx``) whose column layout is deliberately arbitrary
(German headers, out of client-field order), proving the import is mapping-driven
rather than positional. Every test uses a session against a fresh in-memory SQLite
database built from ``Base.metadata`` — the same pattern as ``test_models.py``.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy.orm import sessionmaker

from newspulse.clients import (
    ImportValidationError,
    create_client,
    deactivate_client,
    import_clients,
    list_clients,
    preview_import,
    update_client,
)
from newspulse.db import make_engine
from newspulse.models import Analysis, Article, Base, Category, Client

_FIXTURE = Path(__file__).parent / "fixtures" / "clients_sample.xlsx"

# The explicit source-column -> client-field mapping for the fixture sheet.
_MAPPING = {
    "Firmenname": "name",
    "Auch bekannt als": "aliases",
    "Branche": "industry",
    "Land": "country",
    "Suchbegriffe": "keywords",
    "Alarm-Themen": "alert_topics",
}


@pytest.fixture
def session():
    """A session against a fresh in-memory SQLite database."""
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as sess:
        yield sess


# --- Clean import --------------------------------------------------------------


def test_import_creates_all_clients_from_fixture(session):
    result = import_clients(_FIXTURE, _MAPPING, session)

    assert result.created == 3
    assert result.updated == 0
    assert session.query(Client).count() == 3


def test_import_maps_columns_by_field_not_position(session):
    """The arbitrary sheet layout lands in the right fields via the mapping."""
    import_clients(_FIXTURE, _MAPPING, session)

    beispiel = session.query(Client).filter(Client.name == "Beispiel AG").one()
    assert beispiel.industry == "Automotive"
    assert beispiel.country == "DE"
    assert beispiel.aliases == ["Beispiel", "Beispiel Aktiengesellschaft"]
    assert beispiel.keywords == ["elektro", "batterie"]
    assert beispiel.alert_topics == ["Rückruf", "Insolvenz"]


# --- Array-field parsing -------------------------------------------------------


def test_array_fields_trim_whitespace_and_drop_blanks(session):
    """'Test ; ; Test Societas ,' -> two trimmed entries, empty parts dropped."""
    import_clients(_FIXTURE, _MAPPING, session)

    test_se = session.query(Client).filter(Client.name == "Test SE").one()
    assert test_se.aliases == ["Test", "Test Societas"]
    # A blank delimited cell parses to an empty list, not [""].
    assert test_se.keywords == []
    assert test_se.alert_topics == []


def test_array_fields_split_on_comma_or_semicolon(session):
    """Comma-delimited keywords and semicolon-delimited alert topics both parse."""
    rows = preview_import(_FIXTURE, _MAPPING)

    beispiel = next(r for r in rows if r["name"] == "Beispiel AG")
    assert beispiel["keywords"] == ["elektro", "batterie"]  # comma
    assert beispiel["alert_topics"] == ["Rückruf", "Insolvenz"]  # semicolon


# --- Preview does not write ----------------------------------------------------


def test_preview_import_returns_rows_without_writing(session):
    rows = preview_import(_FIXTURE, _MAPPING)

    assert len(rows) == 3
    assert rows[0]["name"] == "Beispiel AG"
    # Nothing was committed.
    assert session.query(Client).count() == 0


# --- Re-import: no duplicates --------------------------------------------------


def test_reimport_updates_existing_clients_without_duplicating(session):
    import_clients(_FIXTURE, _MAPPING, session)
    result = import_clients(_FIXTURE, _MAPPING, session)

    assert result.created == 0
    assert result.updated == 3
    assert session.query(Client).count() == 3


def test_reimport_matches_name_case_insensitively_and_trimmed(session):
    """A pre-existing client under a differently-cased, padded name is updated,
    not duplicated — the fixture's '  Muster GmbH  ' matches 'muster gmbh'."""
    create_client(session, name="muster gmbh", industry="Alt")

    result = import_clients(_FIXTURE, _MAPPING, session)

    # Beispiel AG + Test SE are new; Muster GmbH matches the seeded client.
    assert result.created == 2
    assert result.updated == 1
    assert session.query(Client).count() == 3
    muster = session.query(Client).filter(Client.industry == "Technologie").one()
    assert muster.name.strip() == "Muster GmbH"


def test_reimport_with_umlaut_name_updates_not_duplicates(session, tmp_path):
    """A German name with an uppercase umlaut re-imports onto the existing client.
    SQLite's lower()/trim() fold only ASCII, so 'Öko AG' would slip a SQL match and
    duplicate; the dedup is done in Python with a full Unicode casefold."""
    csv = tmp_path / "umlaut.csv"
    csv.write_text("Firmenname,Branche\nÖko AG,Energie\n", encoding="utf-8")
    mapping = {"Firmenname": "name", "Branche": "industry"}

    first = import_clients(csv, mapping, session)
    second = import_clients(csv, mapping, session)

    assert first.created == 1
    assert second.created == 0
    assert second.updated == 1
    assert session.query(Client).count() == 1


def test_import_matches_umlaut_case_variant_of_existing_client(session, tmp_path):
    """A client created via the CRUD service under an uppercase-umlaut name is
    matched (not duplicated) when a sheet imports it in a different case — the fold
    happens in Python, which SQLite's ASCII-only lower() cannot do."""
    create_client(session, name="Öko AG", industry="Alt")

    csv = tmp_path / "umlaut_variant.csv"
    csv.write_text("Firmenname,Branche\nöko ag,Neu\n", encoding="utf-8")
    result = import_clients(csv, {"Firmenname": "name", "Branche": "industry"}, session)

    assert result.created == 0
    assert result.updated == 1
    assert session.query(Client).count() == 1


# --- Bad sheet: missing name column --------------------------------------------


def test_import_missing_name_column_raises_clear_error(session, tmp_path):
    """A sheet with no name column (mapped to an absent header) is rejected."""
    bad = tmp_path / "no_name.xlsx"
    pd.DataFrame([{"Branche": "Automotive", "Land": "DE"}]).to_excel(
        bad, index=False, engine="openpyxl"
    )

    mapping = {"Firmenname": "name", "Branche": "industry"}
    with pytest.raises(ImportValidationError, match="Firmenname"):
        import_clients(bad, mapping, session)
    assert session.query(Client).count() == 0


def test_import_unmapped_name_field_raises(session):
    """A mapping that never targets 'name' is rejected before any write."""
    mapping = {"Branche": "industry", "Land": "country"}
    with pytest.raises(ImportValidationError, match="name"):
        preview_import(_FIXTURE, mapping)


def test_import_row_with_blank_name_names_the_row(tmp_path):
    """An empty name cell raises an error naming the offending spreadsheet row."""
    bad = tmp_path / "blank_name.csv"
    bad.write_text("Firmenname,Land\nBeispiel AG,DE\n,DE\n", encoding="utf-8")

    with pytest.raises(ImportValidationError, match="Row 3"):
        preview_import(bad, {"Firmenname": "name", "Land": "country"})


# --- CSV path ------------------------------------------------------------------


def test_import_reads_csv(session, tmp_path):
    """The same import path handles a .csv, splitting the delimited alias cell."""
    csv = tmp_path / "clients.csv"
    csv.write_text(
        "Firmenname,Auch bekannt als\nDelta AG,Delta; Delta Gruppe\n",
        encoding="utf-8",
    )

    import_clients(csv, {"Firmenname": "name", "Auch bekannt als": "aliases"}, session)

    delta = session.query(Client).filter(Client.name == "Delta AG").one()
    assert delta.aliases == ["Delta", "Delta Gruppe"]


# --- CRUD service --------------------------------------------------------------


def test_create_client_persists_with_defaults(session):
    client = create_client(session, name="Neu GmbH")

    assert client.id is not None
    assert client.active is True
    assert client.country == "DE"
    assert client.aliases == []


def test_update_client_changes_fields(session):
    client = create_client(session, name="Alt AG", industry="Alt")

    update_client(session, client.id, industry="Neu", keywords=["x"])

    reloaded = session.get(Client, client.id)
    assert reloaded.industry == "Neu"
    assert reloaded.keywords == ["x"]


def test_update_unknown_client_raises(session):
    with pytest.raises(LookupError):
        update_client(session, 999, industry="x")


def test_update_rejects_unknown_field(session):
    client = create_client(session, name="Alt AG")
    with pytest.raises(ValueError):
        update_client(session, client.id, bogus="x")


def test_list_clients_excludes_inactive_by_default(session):
    create_client(session, name="Aktiv AG")
    inactive = create_client(session, name="Ruhend AG")
    deactivate_client(session, inactive.id)

    names = [c.name for c in list_clients(session)]
    assert names == ["Aktiv AG"]
    all_names = {c.name for c in list_clients(session, include_inactive=True)}
    assert all_names == {"Aktiv AG", "Ruhend AG"}


def test_deactivate_is_soft_and_keeps_articles_and_analyses(session):
    """Deactivating a client sets active=False but keeps its archive intact."""
    client = create_client(session, name="Portfolio AG")
    article = Article(
        title="Portfolio AG meldet Rekordumsatz",
        url="https://example.de/portfolio-1",
        source="Handelsblatt",
        published_at=_utc(),
        fetched_at=_utc(),
        summary_text="Kurzfassung.",
        language="de",
        title_hash="hash-1",
    )
    session.add(article)
    session.flush()
    session.add(
        Analysis(
            article_id=article.id,
            client_id=client.id,
            category=Category.FINANZEN,
            relevance_score=8,
            importance_score=7,
            is_alert=True,
        )
    )
    session.commit()

    deactivate_client(session, client.id)

    reloaded = session.get(Client, client.id)
    assert reloaded.active is False
    # The archive is permanent: article and analysis both survive deactivation.
    assert session.query(Article).count() == 1
    assert session.query(Analysis).filter_by(client_id=client.id).count() == 1


def _utc():
    import datetime as dt

    return dt.datetime(2026, 7, 24, 8, 0, tzinfo=dt.UTC)
