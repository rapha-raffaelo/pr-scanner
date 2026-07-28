"""FastAPI application factory for the NewsPulse dashboard.

Server-rendered (Jinja + HTMX, no build step) per DEC-3. The app is created by
``create_app()`` so tests can build a fresh instance and override the database
session dependency to point at a seeded fixture database — nothing here reaches
for the process-wide engine at import time.
"""

from __future__ import annotations

import datetime as dt
import re
import urllib.parse
from collections.abc import Iterator
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .. import branding
from ..db import get_session

# The web package ships its own templates/ and static/ next to this module, so
# resolve them relative to the file rather than the process working directory.
_WEB_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _WEB_DIR / "templates"
_STATIC_DIR = _WEB_DIR / "static"

# URL schemes safe to emit into an href. Article URLs come from external RSS
# feeds (attacker-influenced), and Jinja autoescape neutralizes <>&" but NOT the
# javascript:/data: schemes — such a URL in an href would run script in the app's
# origin. A relative/scheme-less URL is harmless (same-origin navigation, no code
# execution), so it is allowed through; anything with a non-listed scheme is
# blanked. mailto is included for a future "email this" affordance.
_SAFE_URL_SCHEMES = frozenset({"http", "https", "mailto"})

# Browsers strip ASCII control chars and spaces from a URL before parsing its
# scheme, so "java\tscript:alert(1)" executes as javascript:. Strip the same set
# before the scheme check to close that bypass. C0 controls (0x00–0x1f) + space.
_URL_STRIP_CHARS = re.compile(r"[\x00-\x20]")


def safe_url(value: str | None) -> str:
    """Return ``value`` for rendering into an href only if its scheme is allowed.

    A ``javascript:``/``data:`` (or any non-http/mailto) URL is blanked to ``""``
    so a feed-sourced link can never become a script-executing anchor. Relative
    and scheme-less URLs pass through unchanged.
    """
    if not value:
        return ""
    scheme = urllib.parse.urlparse(_URL_STRIP_CHARS.sub("", value)).scheme.lower()
    if scheme and scheme not in _SAFE_URL_SCHEMES:
        return ""
    return value


# German day/month names, spelled out here rather than via ``locale``: a de_DE
# locale is not installed on every host (and is absent from most containers), and
# setlocale is process-global, so a missing locale would silently fall back to
# English dates in a German-language UI.
_DE_WEEKDAYS = (
    "Montag",
    "Dienstag",
    "Mittwoch",
    "Donnerstag",
    "Freitag",
    "Samstag",
    "Sonntag",
)
_DE_MONTHS = (
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
)


def de_long_date(value: dt.date | dt.datetime) -> str:
    """Format a date the way the header shows it: ``Mittwoch, 22. Juli 2026``."""
    return (
        f"{_DE_WEEKDAYS[value.weekday()]}, {value.day}. "
        f"{_DE_MONTHS[value.month - 1]} {value.year}"
    )


# Image types a stored logo may carry. Deliberately the raster set only: an SVG
# is executable markup, and inlining one into the page would hand the site it
# came from script execution in this origin.
_LOGO_DATA_RE = re.compile(
    r"^data:image/(png|jpeg|webp|gif|x-icon);base64,[A-Za-z0-9+/=]+$"
)


def logo_src(value: str | None) -> str:
    """A logo URL safe to put in an ``<img src>``.

    Distinct from :func:`safe_url`, which blanks every non-http scheme — correct
    for a feed-supplied link, wrong here, because a fetched logo is stored as a
    ``data:`` URI precisely so the dashboard makes no outbound request. Only the
    raster ``data:image/*;base64`` shape this app writes is allowed through, plus
    an ordinary https URL an operator may have pasted.
    """
    raw = (value or "").strip()
    if not raw:
        return ""
    if _LOGO_DATA_RE.match(raw):
        return raw
    return raw if raw.lower().startswith("https://") else ""


def assistant_context(request) -> dict:
    """The drawer's query parameters, derived from the page being viewed.

    Built here rather than in each route so a new page inherits a working
    assistant automatically, and so the context can never disagree with the URL
    the reader is actually on.
    """
    params: dict[str, str] = {}
    path = request.url.path
    if path.startswith("/client/"):
        segment = path.split("/")[2] if len(path.split("/")) > 2 else ""
        if segment.isdigit():
            params["client_id"] = segment
    if path == "/":
        day = request.query_params.get("date")
        if day:
            params["date"] = day
    client = request.query_params.get("client")
    if path == "/" and client and client.isdigit():
        params["client_id"] = client
    return params



# One Jinja environment for the whole app, shared with the route modules via the
# app instance (see ``create_app``). Kept module-level so it is built once.
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
# Scheme allow-list filter used by every template that renders a feed URL into an
# href (see safe_url); registered here so all templates share one definition.
templates.env.filters["safe_url"] = safe_url
# The shared header renders a long German date on every page; registering it here
# keeps one definition for all templates.
templates.env.filters["de_long_date"] = de_long_date
# Client identity: a monogram + stable colour stand in wherever no logo is set,
# so the portfolio never looks half-configured.
templates.env.filters["logo_src"] = logo_src
# The drawer lives in the shared layout, so its context is a global rather than
# something every route has to remember to pass.
templates.env.globals["assistant_ctx"] = assistant_context
templates.env.filters["monogram"] = branding.monogram
templates.env.filters["brand_colour"] = branding.colour


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
    # modules import ``get_db``/``templates`` from this module.
    from .routes import advisory, archive, assistant, client, settings, today, triage

    app.include_router(today.router)
    app.include_router(client.router)
    app.include_router(archive.router)
    app.include_router(settings.router)
    app.include_router(advisory.router)
    app.include_router(assistant.router)
    app.include_router(triage.router)
    return app


def main() -> None:
    """Console entry point: start the dashboard with uvicorn (``newspulse-web``).

    uvicorn is imported here, not at module top, so importing this module for the
    route tests (which drive the app through Starlette's in-process TestClient)
    never requires an ASGI server to be installed.
    """
    import uvicorn

    from .. import config

    uvicorn.run(create_app(), host=config.WEB_HOST, port=config.WEB_PORT)


__all__ = ["create_app", "get_db", "logo_src", "main", "safe_url", "templates"]
