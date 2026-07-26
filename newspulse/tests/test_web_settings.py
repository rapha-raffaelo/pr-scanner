"""Route tests for the Settings view (NP-09).

These drive the settings routes through FastAPI's TestClient against a seeded
in-memory SQLite database — interface-level, not the whole app stack. The
``get_db`` dependency is overridden to hand each request a session bound to the
fixture engine (StaticPool keeps them all on one connection), so no real database
file, daily job, or ``claude`` subprocess is ever involved.

The three story-mandated behaviors are covered explicitly:
* adding a client through the NP-02 CRUD service,
* editing the alert threshold (persisted to the settings table and reloaded),
* an import preview that reports a validation error without committing.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import config
from newspulse.models import (
    Analysis,
    Article,
    Base,
    Category,
    Client,
    Run,
    RunStatus,
    Setting,
)
from newspulse.web.app import create_app, get_db

_ALERT_THRESHOLD_KEY = "alert_threshold"
_ACTIVE_FEEDS_KEY = "active_feeds"


@pytest.fixture
def factory():
    """A sessionmaker bound to a fresh in-memory database with the schema built.

    StaticPool keeps every session on the same single connection so a POST's write
    is visible to the follow-up GET's session — a plain ``:memory:`` engine would
    give each connection its own empty database.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def client(factory):
    """A TestClient whose route session is bound to the fixture database."""
    app = create_app()

    def _override_get_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


def _utc(day: int = 20, hour: int = 6) -> dt.datetime:
    return dt.datetime(2026, 7, day, hour, 0, tzinfo=dt.UTC)


def _seed_analysis(session, *, importance: int, is_alert: bool) -> int:
    """Seed one client + article + analysis, returning the analysis id."""
    client_obj = Client(name="Alpha AG")
    session.add(client_obj)
    session.flush()
    article = Article(
        title="Alpha meldet etwas",
        url="https://ex.de/alpha",
        source="FAZ",
        published_at=_utc(),
        fetched_at=_utc(),
        summary_text="Snippet.",
        language="de",
        title_hash="hash-alpha",
    )
    session.add(article)
    session.flush()
    analysis = Analysis(
        article_id=article.id,
        client_id=client_obj.id,
        category=Category.PRODUKT,
        relevance_score=importance,
        importance_score=importance,
        is_alert=is_alert,
    )
    session.add(analysis)
    session.commit()
    return analysis.id


# --- The page renders ----------------------------------------------------------


def test_settings_page_renders_with_empty_database(client):
    """A fresh install renders the settings page (no clients, feeds from config)."""
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "Einstellungen" in resp.text
    # The registered feeds are listed even before anything is configured.
    assert "Spiegel" in resp.text


def test_threshold_defaults_to_config_when_unset(client):
    """With no stored threshold the page shows the config default as selected."""
    body = client.get("/settings").text
    assert f'value="{config.ALERT_THRESHOLD}" selected' in body


# --- Client CRUD ---------------------------------------------------------------


def test_adding_a_client_creates_it_through_the_service(factory, client):
    """Posting the add form creates a client and it appears in the reloaded list."""
    resp = client.post(
        "/settings/clients",
        data={
            "name": "Neu AG",
            "industry": "Automotive",
            "aliases": "Neu, Neu Gruppe",
            "alert_topics": "Rückruf; Insolvenz",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "Neu AG" in resp.text

    with factory() as session:
        created = session.query(Client).filter(Client.name == "Neu AG").one()
        assert created.active is True
        assert created.aliases == ["Neu", "Neu Gruppe"]
        assert created.alert_topics == ["Rückruf", "Insolvenz"]


def test_adding_a_client_without_name_shows_inline_error(factory, client):
    """A blank name is rejected inline and nothing is written."""
    resp = client.post("/settings/clients", data={"name": "  "})
    assert resp.status_code == 200
    assert "Name ist erforderlich" in resp.text
    with factory() as session:
        assert session.query(Client).count() == 0


def test_duplicate_active_client_name_shows_inline_error(factory, client):
    """A duplicate active name (NP-02 guard) surfaces as an inline error."""
    client.post("/settings/clients", data={"name": "Doppel AG"}, follow_redirects=True)
    resp = client.post("/settings/clients", data={"name": "doppel ag"})
    assert resp.status_code == 200
    assert "already exists" in resp.text
    with factory() as session:
        assert session.query(Client).count() == 1


def test_editing_a_client_updates_fields(factory, client):
    """The per-client edit form updates fields through the CRUD service."""
    with factory() as session:
        obj = Client(name="Alt AG", industry="Alt")
        session.add(obj)
        session.commit()
        client_id = obj.id

    client.post(
        f"/settings/clients/{client_id}",
        data={"name": "Alt AG", "industry": "Neu", "keywords": "x, y"},
        follow_redirects=True,
    )

    with factory() as session:
        reloaded = session.get(Client, client_id)
        assert reloaded.industry == "Neu"
        assert reloaded.keywords == ["x", "y"]


def test_deactivating_a_client_is_soft(factory, client):
    """Deactivate flips active=False; the client row still exists (soft delete)."""
    with factory() as session:
        obj = Client(name="Ruhend AG")
        session.add(obj)
        session.commit()
        client_id = obj.id

    client.post(f"/settings/clients/{client_id}/deactivate", follow_redirects=True)

    with factory() as session:
        assert session.get(Client, client_id).active is False


def test_reactivating_a_client_flips_active_back(factory, client):
    """A deactivated client can be reactivated through the edit service."""
    with factory() as session:
        obj = Client(name="Wieder AG", active=False)
        session.add(obj)
        session.commit()
        client_id = obj.id

    client.post(f"/settings/clients/{client_id}/reactivate", follow_redirects=True)

    with factory() as session:
        assert session.get(Client, client_id).active is True


# --- Alert threshold -----------------------------------------------------------


def test_editing_alert_threshold_persists_and_reloads(factory, client):
    """Editing the threshold persists it to the settings table and reloads it."""
    resp = client.post(
        "/settings/threshold", data={"alert_threshold": "9"}, follow_redirects=True
    )
    assert resp.status_code == 200

    # Persisted to the settings table.
    with factory() as session:
        stored = session.get(Setting, _ALERT_THRESHOLD_KEY)
        assert stored is not None
        assert stored.value == "9"

    # Reloaded and shown as the selected option on the page.
    body = client.get("/settings").text
    assert 'value="9" selected' in body


def test_threshold_is_clamped_to_the_score_scale(factory, client):
    """An out-of-range threshold is clamped to the 0..10 importance scale."""
    client.post("/settings/threshold", data={"alert_threshold": "42"}, follow_redirects=True)
    with factory() as session:
        assert session.get(Setting, _ALERT_THRESHOLD_KEY).value == "10"


def test_non_numeric_threshold_shows_inline_error(factory, client):
    """A non-numeric threshold is rejected inline and nothing is persisted."""
    resp = client.post("/settings/threshold", data={"alert_threshold": "hoch"})
    assert resp.status_code == 200
    assert "muss eine Zahl sein" in resp.text
    with factory() as session:
        assert session.get(Setting, _ALERT_THRESHOLD_KEY) is None


def test_changing_threshold_does_not_rewrite_stored_analyses(factory, client):
    """Changing the threshold affects only future flagging, never stored analyses.

    An analysis stored with is_alert=False and importance 5 must stay is_alert=False
    after the threshold drops to 3 — writing the setting touches no analyses row.
    """
    with factory() as session:
        analysis_id = _seed_analysis(session, importance=5, is_alert=False)

    client.post("/settings/threshold", data={"alert_threshold": "3"}, follow_redirects=True)

    with factory() as session:
        assert session.get(Analysis, analysis_id).is_alert is False


# --- Feeds ---------------------------------------------------------------------


def test_saving_active_feeds_persists_the_checked_subset(factory, client):
    """Saving the feed form persists exactly the checked feed names to settings."""
    client.post(
        "/settings/feeds", data={"feed": ["Spiegel", "Zeit"]}, follow_redirects=True
    )
    with factory() as session:
        stored = session.get(Setting, _ACTIVE_FEEDS_KEY)
        assert stored is not None
        assert set(json.loads(stored.value)) == {"Spiegel", "Zeit"}


# --- Import: preview before commit ---------------------------------------------


def test_import_preview_reports_validation_error_without_committing(factory, client):
    """A sheet with no name column surfaces the NP-02 error inline, writes nothing."""
    csv_bytes = b"Branche,Land\nAutomotive,DE\n"
    resp = client.post(
        "/settings/import/preview",
        data={"map_name": "Firmenname", "map_industry": "Branche"},
        files={"file": ("clients.csv", csv_bytes, "text/csv")},
    )
    assert resp.status_code == 200
    # The NP-02 validation error names the missing source column inline.
    assert "Firmenname" in resp.text
    # Nothing was written.
    with factory() as session:
        assert session.query(Client).count() == 0


def test_import_preview_shows_parsed_rows_without_committing(factory, client):
    """A valid sheet previews its parsed rows but commits nothing."""
    csv_bytes = "Firmenname,Branche\nBeispiel AG,Automotive\n".encode("utf-8")
    resp = client.post(
        "/settings/import/preview",
        data={"map_name": "Firmenname", "map_industry": "Branche"},
        files={"file": ("clients.csv", csv_bytes, "text/csv")},
    )
    assert resp.status_code == 200
    assert "Beispiel AG" in resp.text
    with factory() as session:
        assert session.query(Client).count() == 0


def test_import_commit_creates_clients(factory, client):
    """Committing the import writes the clients through the NP-02 importer."""
    csv_bytes = "Firmenname,Branche\nCommit AG,Handel\n".encode("utf-8")
    resp = client.post(
        "/settings/import/commit",
        data={"map_name": "Firmenname", "map_industry": "Branche"},
        files={"file": ("clients.csv", csv_bytes, "text/csv")},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    with factory() as session:
        created = session.query(Client).filter(Client.name == "Commit AG").one()
        assert created.industry == "Handel"


def test_import_without_a_file_shows_inline_error(factory, client):
    """Previewing with no file selected is a clean inline error, not a crash."""
    resp = client.post(
        "/settings/import/preview",
        data={"map_name": "Firmenname"},
    )
    assert resp.status_code == 200
    assert "Datei auswählen" in resp.text
    with factory() as session:
        assert session.query(Client).count() == 0


# --- Run history ---------------------------------------------------------------


def test_run_history_shows_status_articles_and_errors(factory, client):
    """The run-history table surfaces status, articles_found, and any errors."""
    with factory() as session:
        session.add(
            Run(
                started_at=_utc(hour=6),
                finished_at=_utc(hour=6),
                status=RunStatus.PARTIAL,
                articles_found=87,
                errors=["Feed Spiegel timed out"],
            )
        )
        session.commit()

    body = client.get("/settings").text
    assert "partial" in body
    assert "87" in body
    assert "Feed Spiegel timed out" in body
