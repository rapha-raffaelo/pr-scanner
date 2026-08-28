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


def test_the_market_columns_exist_on_the_migrated_clients_table(tmp_path, monkeypatch):
    """The per-mandate mute and the industry verdict, against the real migration.

    Every test that exercises them builds its schema from ``Base.metadata``, so an
    ORM/migration divergence in either column would pass the whole suite and first
    appear on a deployed database — as a 500 on the market page, or as a mute that
    cannot be stored. The array column is round-tripped rather than merely
    present: ``muted_signal_kinds`` is read back by the sweep to decide what not
    to fetch, and a column that cannot hold a list would fail there.
    """
    db_path = tmp_path / "market.db"
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)

    command.upgrade(_alembic_config(), "head")

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        columns = {c["name"] for c in inspect(engine).get_columns("clients")}
        assert {"muted_signal_kinds", "field_usable", "field_checked_at"} <= columns

        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO clients "
                    "(name, aliases, industry, country, keywords, alert_topics, "
                    "muted_signal_kinds, active, created_at) "
                    "VALUES ('Beispiel AG', '[]', 'Automotive', 'DE', '[]', '[]', "
                    ":muted, 1, '2026-07-24 08:00:00')"
                ),
                {"muted": '["studie", "veranstaltung"]'},
            )
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT muted_signal_kinds, field_usable, field_checked_at "
                    "FROM clients"
                )
            ).one()

        assert json.loads(row[0]) == ["studie", "veranstaltung"]
        # Not backfilled, and that is the point: 0 would read as "the press does
        # not write this word" for every existing mandate, which is the false
        # accusation the column was added to prevent.
        assert row[1] is None
        assert row[2] is None
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


def test_the_chain_comes_all_the_way_back_down_and_up_again(tmp_path, monkeypatch):
    """A rollback has to be available before it is needed, not discovered then.

    ``downgrade base`` stopped two migrations short of the schema it was aiming
    at, and the reason is the same in both: ``sa.Enum(create_constraint=True)``
    emits a CHECK named after the *Enum*, SQLite has no ALTER for a column or a
    constraint, and batch mode therefore rebuilds the table from its reflected
    definition — carrying a CHECK on ``tonality`` and on ``triage_state`` into
    tables that no longer had those columns. "no such column: tonality", raised
    by the downgrade of the migration that added it.

    Down and up rather than just down: a teardown that succeeds by dropping
    something the rebuild then cannot recreate is not a working chain, and only
    the return trip says so.
    """
    db_path = tmp_path / "roundtrip.db"
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    cfg = _alembic_config()

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        left = set(inspect(engine).get_table_names()) - {"alembic_version"}
        assert not left, f"the teardown left tables behind: {sorted(left)}"
    finally:
        engine.dispose()

    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        rebuilt = set(inspect(engine).get_table_names())
        assert "clients" in rebuilt and "analyses" in rebuilt
        with engine.connect() as conn:
            head = conn.execute(text("select version_num from alembic_version")).scalar()
        assert head, "the chain came back up without stamping a revision"
    finally:
        engine.dispose()


def test_the_migrated_schema_allows_one_occasion_per_plan_hook(tmp_path, monkeypatch):
    """Asserted against the migrated file, not against ``Base.metadata``.

    ``plan_view.occasion_for`` reads for an occasion and then inserts, in
    FastAPI's threadpool: a double-clicked "Text schreiben" is two requests that
    both see nothing. The index is the only thing that settles it, and the index
    a deployment actually gets is the one the migration writes. Partial, because
    ``plan_hook_id`` is NULL on nearly every impulse on file and a plain unique
    index would allow exactly one of those in the whole table.
    """
    db_path = tmp_path / "one_occasion.db"
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)

    command.upgrade(_alembic_config(), "head")

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        indexes = {ix["name"]: ix for ix in inspect(engine).get_indexes("angles")}
        with engine.connect() as conn:
            ddl = conn.execute(
                text(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type = 'index' AND name = 'ux_angles_plan_hook'"
                )
            ).scalar()
    finally:
        engine.dispose()

    assert "ux_angles_plan_hook" in indexes, sorted(indexes)
    assert indexes["ux_angles_plan_hook"]["column_names"] == ["plan_hook_id"]
    assert indexes["ux_angles_plan_hook"]["unique"]
    assert ddl and "plan_hook_id IS NOT NULL" in ddl, ddl


def test_no_enum_column_is_dropped_without_its_check(tmp_path):
    """The guard for the class rather than for the two that were found.

    A CHECK is named after the Enum, which is not always spelled like the column
    it sits on — ``triage_state`` carries one called ``triagestate`` — so this
    reads the name out of the migration rather than assuming it.
    """
    import re

    offenders = []
    for path in sorted((_PROJECT_ROOT / "migrations" / "versions").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        up = source.split("def upgrade", 1)[-1].split("def downgrade", 1)[0]
        down = source.split("def downgrade", 1)[-1]
        for match in re.finditer(
            r'sa\.Column\(\s*"([a-z_]+)"\s*,\s*sa\.Enum\((.*?)\)\s*,', up, re.S
        ):
            column, args = match.group(1), match.group(2)
            if "create_constraint=True" not in args:
                continue
            named = re.search(r'name="([a-z_]+)"', args)
            constraint = named.group(1) if named else column
            if f'drop_column("{column}")' in down and (
                f'drop_constraint("{constraint}"' not in down
            ):
                offenders.append(f"{path.name}: {column} (CHECK {constraint})")

    assert not offenders, "these downgrades drop a column and keep its CHECK: " + (
        "; ".join(offenders)
    )
