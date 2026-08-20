"""Integration test that exercises the real Alembic migration path.

The unit tests in ``test_models.py`` build the schema from ``Base.metadata`` for
speed. This test instead runs ``alembic upgrade head`` against a throwaway
on-disk SQLite database, so the hand-authored migration — which actually owns the
schema per the house rule ("all schema changes go through Alembic") — is verified,
including the array-field round-trip the acceptance criteria require.
"""

from __future__ import annotations

import json
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from newspulse import config

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _alembic_config() -> Config:
    """Alembic config anchored to absolute paths so it is CWD-independent."""
    cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_PROJECT_ROOT / "migrations"))
    return cfg


def test_alembic_upgrade_creates_schema_and_round_trips_arrays(tmp_path, monkeypatch):
    db_path = tmp_path / "migrated.db"
    # env.py derives the URL from config.DATABASE_PATH at call time, so redirect it
    # at a throwaway file for this run.
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)

    command.upgrade(_alembic_config(), "head")

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        tables = set(inspect(engine).get_table_names())
        assert {
            "clients", "articles", "analyses", "runs", "settings", "angles",
            # The standards the tool writes under are schema, not configuration:
            # without this table an override is silently impossible and every
            # prompt quietly falls back to the shipped text.
            "brain_overrides",
        } <= tables

        # The acceptance-required array round-trip, but against the *migrated*
        # schema rather than the ORM's create_all().
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO clients "
                    "(name, aliases, industry, country, keywords, alert_topics, "
                    "active, created_at) "
                    "VALUES (:name, :aliases, :industry, :country, :keywords, "
                    ":alert_topics, 1, :created_at)"
                ),
                {
                    "name": "Beispiel AG",
                    "aliases": '["Beispiel", "Beispiel AG"]',
                    "industry": "Automotive",
                    "country": "DE",
                    "keywords": '["elektro", "batterie"]',
                    "alert_topics": '["Rückruf"]',
                    "created_at": "2026-07-24 08:00:00",
                },
            )
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT aliases, keywords, alert_topics FROM clients")
            ).one()

        assert json.loads(row[0]) == ["Beispiel", "Beispiel AG"]
        assert json.loads(row[1]) == ["elektro", "batterie"]
        assert json.loads(row[2]) == ["Rückruf"]
    finally:
        engine.dispose()
