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
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from newspulse import config

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _alembic_config() -> Config:
    """Alembic config anchored to absolute paths so it is CWD-independent."""
    cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_PROJECT_ROOT / "migrations"))
    return cfg


def test_the_migration_chain_has_exactly_one_head():
    """Two heads make ``alembic upgrade head`` fail, and they arrive quietly.

    The revision ids are not contiguous — a story numbers its migration after
    itself and parents whatever was at head — so nothing about the filenames says
    whether two sibling branches both hung off the same parent. Merging them is
    when it would otherwise be noticed, on a database that then refuses to
    upgrade. This is the cheap check that catches it at the merge instead.
    """
    heads = ScriptDirectory.from_config(_alembic_config()).get_heads()

    assert len(heads) == 1, f"the migration chain forked: {sorted(heads)}"


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
            # The generated texts. Named here because the hand-authored revision
            # owns this schema, and a format that cannot be stored is six
            # formats that cannot be stored.
            "assets",
            # The standards the tool writes under are schema, not configuration:
            # without this table an override is silently impossible and every
            # prompt quietly falls back to the shipped text.
            "brain_overrides",
            # The three market classes. A separate table is the whole point: a
            # study filed in ``articles`` would make every coverage query in the
            # tool wrong, and nobody would notice until a client report counted a
            # consultation as press.
            "market_signals",
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


def test_a_row_written_before_the_stamp_carries_no_version_rather_than_zero(
    tmp_path, monkeypatch
):
    """AC 2, against the real migration rather than against the ORM.

    The interesting row is one that already exists when ``0023_brain_version``
    runs, so the database is brought up to the revision *before* it, given a
    draft, and only then upgraded. Backfilling zero would have been the easy
    default and it is a lie: zero means "the standards have never been changed
    here", which a row from before anything was recorded cannot claim. NULL is
    the honest answer and the interface renders it as "unbekannt".
    """
    db_path = tmp_path / "stamped.db"
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    cfg = _alembic_config()

    command.upgrade(cfg, "0022_brain_overrides")
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO clients "
                    "(name, aliases, industry, country, keywords, alert_topics, "
                    "active, created_at) "
                    "VALUES ('Alpha AG', '[]', 'Neobroker', 'DE', '[]', '[]', 1, "
                    "'2026-07-24 08:00:00')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO angles "
                    "(client_id, generated_at, subject, message, context) "
                    "VALUES (1, '2026-07-24 08:00:00', 'Betreff', 'Text.', 'Kontext.')"
                )
            )

        command.upgrade(cfg, "head")

        with engine.connect() as conn:
            assert conn.execute(text("SELECT brain_version FROM angles")).scalar() is None
            # Every generator's table, not only the one with a row in it: a
            # column missing from the third would leave that generator unable to
            # stamp at all, which no ORM-built test would notice.
            for table in ("angles", "outreach", "advisories"):
                columns = {c["name"] for c in inspect(engine).get_columns(table)}
                assert "brain_version" in columns, table
            # The letter carries a second model-written text — the cross-check —
            # composed under its own brain prompt after the letter was already
            # written. One column cannot honestly stamp both.
            outreach_columns = {c["name"] for c in inspect(engine).get_columns("outreach")}
            assert "review_brain_version" in outreach_columns
    finally:
        engine.dispose()


def test_a_regulatory_date_in_the_future_survives_the_migrated_schema(
    tmp_path, monkeypatch
):
    """The property the regulatory class exists for, checked against the real
    schema rather than the ORM's ``create_all``.

    A CHECK bounding ``effective_at`` to the past would be the easy default and it
    would empty the calendar: a rule taking effect in 2030 is worth having in 2026
    *because* that date has not arrived. Nothing here may clamp it to now.
    """
    db_path = tmp_path / "future.db"
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)

    command.upgrade(_alembic_config(), "head")

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO clients "
                    "(name, aliases, industry, country, keywords, alert_topics, "
                    "active, created_at) "
                    "VALUES ('Alpha AG', '[]', 'Neobroker', 'DE', '[]', '[]', 1, "
                    "'2026-08-24 06:10:00')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO market_signals "
                    "(client_id, kind, title, publisher, url, found_at, "
                    "effective_at, origin) "
                    "VALUES (1, 'regulierung', 'Verordnung tritt in Kraft', "
                    "'Beispiel-Behoerde', 'https://b.example.de/v1', "
                    "'2026-08-24 06:10:00', '2030-01-01 00:00:00', 'kuratiert')"
                )
            )

        with engine.connect() as conn:
            stored = conn.execute(
                text("SELECT effective_at, origin FROM market_signals")
            ).one()

        assert str(stored[0]).startswith("2030-01-01")
        assert stored[1] == "kuratiert"
    finally:
        engine.dispose()


def test_the_same_market_url_may_belong_to_two_mandates(tmp_path, monkeypatch):
    """"Unique per client", not globally. The same consultation is a real signal
    for every mandate the rule applies to, and each has to be able to mute, read
    and report it on its own."""
    db_path = tmp_path / "scoped.db"
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)

    command.upgrade(_alembic_config(), "head")

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        with engine.begin() as conn:
            for name in ("Alpha AG", "Beta AG"):
                conn.execute(
                    text(
                        "INSERT INTO clients "
                        "(name, aliases, industry, country, keywords, alert_topics, "
                        "active, created_at) "
                        "VALUES (:name, '[]', 'Neobroker', 'DE', '[]', '[]', 1, "
                        "'2026-08-24 06:10:00')"
                    ),
                    {"name": name},
                )
            for client_id in (1, 2):
                conn.execute(
                    text(
                        "INSERT INTO market_signals "
                        "(client_id, kind, title, publisher, url, found_at, origin) "
                        "VALUES (:client_id, 'regulierung', 'Konsultation', "
                        "'Beispiel-Behoerde', 'https://b.example.de/k1', "
                        "'2026-08-24 06:10:00', 'kuratiert')"
                    ),
                    {"client_id": client_id},
                )

        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT COUNT(*) FROM market_signals")
            ).scalar() == 2
    finally:
        engine.dispose()
