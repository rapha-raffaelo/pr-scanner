"""FastAPI application factory for the NewsPulse dashboard.

Server-rendered (Jinja + HTMX, no build step) per DEC-3. The app is created by
``create_app()`` so tests can build a fresh instance and override the database
session dependency to point at a seeded fixture database — nothing here reaches
for the process-wide engine at import time.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..db import get_session

# The web package ships its own templates/ and static/ next to this module, so
# resolve them relative to the file rather than the process working directory.
_WEB_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _WEB_DIR / "templates"
_STATIC_DIR = _WEB_DIR / "static"

# One Jinja environment for the whole app, shared with the route modules via the
# app instance (see ``create_app``). Kept module-level so it is built once.
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def get_db() -> Iterator[Session]:
    """Request-scoped database session.

    A FastAPI dependency so routes never open their own session and tests can
    override it (``app.dependency_overrides[get_db]``) to inject a session bound
    to a seeded in-memory database. The ``with`` block returns the connection to
    the pool deterministically at request end.
    """
    with get_session() as session:
        yield session


def create_app() -> FastAPI:
    """Build the FastAPI app: mount static assets and register the routes."""
    app = FastAPI(title="NewsPulse")
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # Imported here (not at module top) to avoid a circular import: the route
    # module imports ``get_db``/``templates`` from this module.
    from .routes import today

    app.include_router(today.router)
    return app


__all__ = ["create_app", "get_db", "templates"]
