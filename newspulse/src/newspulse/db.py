"""Database engine and session helpers.

Nothing here creates or mutates schema — that is Alembic's job. This module only
opens connections and hands out sessions against a schema that migrations own.
"""

from __future__ import annotations

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import database_url


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    """SQLite ships with foreign-key enforcement off by default; turn it on for
    every connection so the FKs and ON DELETE CASCADE in the models actually bite.
    The guard keeps this a no-op for any non-SQLite backend."""
    if dbapi_connection.__class__.__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def make_engine(url: str | None = None) -> Engine:
    """Create an engine for ``url`` (default: the configured SQLite database)."""
    return create_engine(
        url or database_url(),
        # SQLite guards each connection against cross-thread use; the web app and
        # job may hand a connection across threads, so relax that check.
        connect_args={"check_same_thread": False},
    )


_engine: Engine | None = None


def get_engine() -> Engine:
    """Lazily create and cache the process-wide engine."""
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


def session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    """A configured ``sessionmaker`` bound to ``engine`` (default: the cached one)."""
    return sessionmaker(bind=engine or get_engine(), expire_on_commit=False)


def get_session() -> Session:
    """Open a new session against the process-wide engine."""
    return session_factory()()
