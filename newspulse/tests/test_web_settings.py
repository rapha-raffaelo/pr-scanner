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
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from newspulse import config
from newspulse.feeds import load_feeds
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
from newspulse.web.routes.settings import get_active_feed_names, set_active_feed_names

_ALERT_THRESHOLD_KEY = "alert_threshold"


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


def test_reactivate_into_duplicate_name_shows_inline_error(factory, client):
    """Reactivating into a name a live client already holds is refused inline, so an
    operator cannot create two active same-name clients that would break import
    dedup (the QA-reported bypass through the reactivate route)."""
    client.post("/settings/clients", data={"name": "Alpha AG"}, follow_redirects=True)
    with factory() as session:
        first_id = session.query(Client).filter_by(name="Alpha AG").one().id
    client.post(f"/settings/clients/{first_id}/deactivate", follow_redirects=True)
    client.post("/settings/clients", data={"name": "Alpha AG"}, follow_redirects=True)

    resp = client.post(f"/settings/clients/{first_id}/reactivate")
    assert resp.status_code == 200
    assert "already exists" in resp.text
    with factory() as session:
        assert session.query(Client).filter_by(name="Alpha AG", active=True).count() == 1


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
    feeds = load_feeds()
    with factory() as session:
        assert get_active_feed_names(session, feeds) == {"Spiegel", "Zeit"}


def test_feed_added_after_save_defaults_active(factory):
    """A feed added to the registry after a save is active by default (deny-list).

    The operator saved with only "A" checked out of {A, B}; a later-registered feed
    "C" was never deselected, so it must come back active — not silently dropped.
    """
    registry_v1 = [SimpleNamespace(name="A"), SimpleNamespace(name="B")]
    with factory() as session:
        set_active_feed_names(session, ["A"], [f.name for f in registry_v1])

    registry_v2 = registry_v1 + [SimpleNamespace(name="C")]
    with factory() as session:
        active = get_active_feed_names(session, registry_v2)
    assert active == {"A", "C"}  # B stays off (deselected); C defaults on


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
    csv_bytes = b"Firmenname,Branche\nBeispiel AG,Automotive\n"
    resp = client.post(
        "/settings/import/preview",
        data={"map_name": "Firmenname", "map_industry": "Branche"},
        files={"file": ("clients.csv", csv_bytes, "text/csv")},
    )
    assert resp.status_code == 200
    assert "Beispiel AG" in resp.text
    with factory() as session:
        assert session.query(Client).count() == 0


def test_import_preview_rejects_two_fields_mapped_to_one_column(factory, client):
    """Mapping two fields to the same source column is an inline error, not a silent
    drop of the earlier field (which would mis-report 'name' as unmapped)."""
    csv_bytes = b"Firma,Land\nAlpha AG,DE\n"
    resp = client.post(
        "/settings/import/preview",
        data={"map_name": "Firma", "map_industry": "Firma"},
        files={"file": ("clients.csv", csv_bytes, "text/csv")},
    )
    assert resp.status_code == 200
    assert "Firma" in resp.text
    assert "map each column" in resp.text
    with factory() as session:
        assert session.query(Client).count() == 0


def test_import_commit_creates_clients(factory, client):
    """Committing the import writes the clients through the NP-02 importer."""
    csv_bytes = b"Firmenname,Branche\nCommit AG,Handel\n"
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


# --- Run trigger + backfill ----------------------------------------------------


def test_run_trigger_rejects_a_window_not_offered(client):
    """Only the offered backfill windows are accepted; anything else is refused
    without starting a sweep."""
    resp = client.post("/settings/run", data={"since_days": "999"})
    assert resp.status_code == 200  # re-rendered with an inline error, no redirect
    assert "Ungültiger Zeitraum" in resp.text


def test_run_trigger_rejects_non_numeric_window(client):
    resp = client.post("/settings/run", data={"since_days": "dreissig"})
    assert resp.status_code == 200
    assert "Ungültiger Zeitraum" in resp.text


def test_run_trigger_refuses_a_second_concurrent_run(client, monkeypatch):
    """A sweep already in flight is reported, never queued: two concurrent runs
    would double-fetch every feed and race on the same articles."""
    from newspulse.web.routes import settings as settings_routes

    # Hold the guard as an in-flight run would, without starting a real sweep.
    assert settings_routes._run_guard.acquire(blocking=False)
    try:
        resp = client.post("/settings/run", data={"since_days": ""})
        assert resp.status_code == 200
        assert "Es läuft bereits ein Lauf" in resp.text
    finally:
        settings_routes._run_guard.release()


def test_run_trigger_starts_a_sweep_with_the_requested_window(client, monkeypatch):
    """A valid request starts the sweep off-thread and redirects; the chosen
    window reaches job.run as an explicit `since`."""
    import datetime as dt

    from newspulse.web.routes import settings as settings_routes

    captured: dict[str, object] = {}

    def _fake_execute(since_days):
        captured["since_days"] = since_days
        settings_routes._run_guard.release()

    class _ImmediateThread:
        def __init__(self, target, args, daemon=None, name=None):
            self._target, self._args = target, args

        def start(self):
            self._target(*self._args)

    monkeypatch.setattr(settings_routes, "_execute_run", _fake_execute)
    monkeypatch.setattr(settings_routes.threading, "Thread", _ImmediateThread)

    resp = client.post(
        "/settings/run", data={"since_days": "30"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings?started=30"
    assert captured["since_days"] == 30
    # The guard is free again for the next run.
    assert settings_routes._run_guard.acquire(blocking=False)
    settings_routes._run_guard.release()


def test_lookback_since_matches_the_requested_window():
    """The CLI's --since-days and the dashboard's control share one definition."""
    import datetime as dt

    from newspulse import job

    fixed = dt.datetime(2026, 7, 27, 12, 0, tzinfo=dt.UTC)
    assert job.lookback_since(30, now=lambda: fixed) == fixed - dt.timedelta(days=30)
    assert job.lookback_since(1, now=lambda: fixed) == fixed - dt.timedelta(days=1)
    with pytest.raises(ValueError):
        job.lookback_since(0)
