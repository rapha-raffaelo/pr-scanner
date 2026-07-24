"""Database engine and session helpers.

Nothing here creates or mutates schema — that is Alembic's job. This module only
opens connections and hands out sessions against a schema that migrations own.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import database_url


def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    """SQLite ships with foreign-key enforcement off by default; turn it on for
    every connection so the FKs and ON DELETE CASCADE in the models actually bite.

    Attached only to NewsPulse's own SQLite engines (see ``make_engine``), never
    to the global ``Engine`` class, so importing this module has no process-wide
    side effect on other SQLAlchemy engines in the same interpreter."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def make_engine(url: str | None = None) -> Engine:
    """Create an engine for ``url`` (default: the configured SQLite database)."""
    resolved_url = url or database_url()
    kwargs: dict[str, object] = {}
    if resolved_url.startswith("sqlite"):
        # SQLite guards each connection against cross-thread use; the web app and
        # job may hand a connection across threads, so relax that check. This kwarg
        # is SQLite-specific, so only pass it for SQLite URLs (other DBAPIs raise
        # TypeError on an unknown connect arg).
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(resolved_url, **kwargs)
    if resolved_url.startswith("sqlite"):
        # Register the PRAGMA on this specific engine only.
        event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    return engine


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


@contextmanager
def get_session() -> Iterator[Session]:
    """Open a managed session against the process-wide engine.

    Used as a context manager so the connection is returned to the pool
    deterministically at scope exit rather than leaking until GC::

        with get_session() as session:
            ...
    """
    session = session_factory()()
    try:
        yield session
    finally:
        session.close()
