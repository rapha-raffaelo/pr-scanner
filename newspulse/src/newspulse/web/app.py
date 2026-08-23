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

from fastapi import Depends, FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .. import branding, config, gnews, i18n, onboarding
from ..db import get_session
from . import google_auth, navigation, runlock
from .auth import BasicAuthMiddleware, is_loopback, require_auth_for_public_bind

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


def _local(value: dt.datetime) -> dt.datetime:
    """A stored timestamp in the reader's zone.

    Every timestamp in the database is UTC (``UTCDateTime``), so rendering one
    without converting shows the container's clock — which is UTC on a PaaS host,
    and rendered a 10:00 sweep as "Letzter Lauf 08:00 Uhr". Doing it in the
    filter means the *view* need not: a route may hand over a value already in
    the reader's zone (the Today feed does, because it also groups by local day)
    and ``astimezone`` on it is a no-op.
    """
    return value.astimezone(config.local_zone())


def de_time(value: dt.datetime) -> str:
    """Clock time in the reader's zone: ``08:15``."""
    return _local(value).strftime("%H:%M")


def de_datetime(value: dt.datetime) -> str:
    """Date and clock time in the reader's zone: ``22.07.2026 08:15``."""
    return _local(value).strftime("%d.%m.%Y %H:%M")


def de_date(value: dt.datetime) -> str:
    """Date alone in the reader's zone: ``22.07.2026``.

    For the places where the clock time is noise rather than information — a
    relationship timeline reads as a sequence of days, and "12.08.2026 09:41" on
    a line about last month invites the reader to weigh a minute that means
    nothing. The letter card keeps :func:`de_datetime`: there the hour is part of
    the release record.
    """
    return _local(value).strftime("%d.%m.%Y")


def de_when(value: dt.datetime) -> str:
    """A timestamp as the header has to read it: the clock alone only for today.

    The header prints the reader's date on one line and the last sweep on the
    next, and the sweep line used to carry a bare "06:15 Uhr". On the morning
    after a sweep that worked, that is exactly right. Three days after the last
    one it says "Letzter Lauf 06:15 Uhr · Lauf ok · 84 neue Artikel" under
    today's date, and the only honest reading of that sentence is that the tool
    ran this morning and the week was quiet. It had not run since Tuesday.

    So the clock stands alone only when the day is today. Anything older names
    its day, because a media monitor that has not run is the news on the page.
    """
    local = _local(value)
    days = (dt.datetime.now(config.local_zone()).date() - local.date()).days
    if days <= 0:
        return f"{local:%H:%M} Uhr"
    if days == 1:
        return f"gestern {local:%H:%M} Uhr"
    return f"{local:%d.%m.}, {local:%H:%M} Uhr"


def run_age_days(value: dt.datetime) -> int:
    """Whole days between that sweep and today, in the reader's zone."""
    local = _local(value)
    return (dt.datetime.now(config.local_zone()).date() - local.date()).days


def de_short_date(value: dt.datetime) -> str:
    """Day and month in the reader's zone, as coverage lists cite it: ``22.07.``"""
    return _local(value).strftime("%d.%m.")


def de_date(value: dt.datetime) -> str:
    """A calendar date in the reader's zone: ``22.07.2026``.

    The report's dates need the year — a document is read months after it was
    sent, which is exactly when ``22.07.`` stops being enough.
    """
    return _local(value).strftime("%d.%m.%Y")


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


def request_language(request) -> str:
    """The reader's interface language, from the cookie set by the toggle."""
    return i18n.normalize(request.cookies.get(i18n.COOKIE_NAME))


def translator(request):
    """``t(text)`` bound to this request's language, for the templates.

    A callable rather than a filter so a template reads ``t("Heute")`` — the
    German source string is right there, which keeps the markup legible without
    a second file to consult.
    """
    language = request_language(request)
    return lambda text: i18n.translate(text, language)


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
# Every rendered timestamp goes through one of these, so the reader's zone is
# applied once, in one place, instead of per template (see _local).
templates.env.filters["de_time"] = de_time
templates.env.filters["de_datetime"] = de_datetime
templates.env.filters["de_date"] = de_date
templates.env.filters["de_short_date"] = de_short_date
templates.env.filters["de_date"] = de_date
# The header's own reading of a timestamp: see de_when.
templates.env.filters["de_when"] = de_when
templates.env.filters["run_age_days"] = run_age_days
# Client identity: a monogram + stable colour stand in wherever no logo is set,
# so the portfolio never looks half-configured.
templates.env.filters["logo_src"] = logo_src
# The drawer lives in the shared layout, so its context is a global rather than
# something every route has to remember to pass.
templates.env.globals["assistant_ctx"] = assistant_context
# A global rather than a context key, because the header is in the shared layout
# and every route would otherwise have to remember to pass it — the way to end up
# with one page that never shows the spinner. Called per render, so it reflects
# the moment the page was built.
templates.env.globals["run_active"] = runlock.is_running
# Used by the competitor picker to group "same field" from "another industry".
# A template global rather than page context: the grouping is a property of the
# two companies, not of the view, and it must not pull a route module in here.
templates.env.globals["shares_field"] = gnews.same_field
templates.env.globals["translator"] = translator
templates.env.globals["request_language"] = request_language
templates.env.globals["LANGUAGES"] = i18n.LANGUAGES
# The sidebar lists the mandates on every page, so the roster is a global for
# the same reason ``run_active`` is: the shared layout needs it and no route
# should have to remember to pass it.
templates.env.globals["nav_clients"] = navigation.nav_clients
# Who is signed in, for the sidebar footer. A global for the same reason as the
# roster: the shared layout needs it and no route should have to pass it.
templates.env.globals["signed_in_as"] = lambda request: request.scope.get("user_email")
templates.env.globals["google_login_active"] = google_auth.is_configured
templates.env.filters["monogram"] = branding.monogram
templates.env.filters["brand_colour"] = branding.colour
# A list answer in the questionnaire is one text column, one entry per line. The
# split lives in ``onboarding`` rather than in the template so the chip a reader
# deletes and the entry the route removes are indexed by the same rule.
templates.env.filters["kickoff_entries"] = onboarding.entries


def get_db() -> Iterator[Session]:
    """Request-scoped database session.

    A FastAPI dependency so routes never open their own session and tests can
    override it (``app.dependency_overrides[get_db]``) to inject a session bound
    to a seeded in-memory database. The ``with`` block returns the connection to
    the pool deterministically at request end.
    """
    with get_session() as session:
        yield session


def stash_db(request: Request, session: Session = Depends(get_db)) -> None:
    """Put the request's session where the shared layout can reach it.

    Registered as an app-wide dependency, and depending on ``get_db`` rather
    than opening its own session is the whole point: FastAPI caches a
    dependency per request, so this is the *same* session the route is using,
    and an override in a test reaches the sidebar too.
    """
    request.state.db = session


def create_app() -> FastAPI:
    """Build the FastAPI app: mount static assets and register the routes."""
    app = FastAPI(title="NewsPulse", dependencies=[Depends(stash_db)])
    # Added first so it wraps every route, including the SSE stream and the
    # static mount's siblings.
    app.add_middleware(BasicAuthMiddleware)
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # Imported here (not at module top) to avoid a circular import: the route
    # modules import ``get_db``/``templates`` from this module.
    from .routes import (
        advisory, archive, assets_view, assistant, client, contacts, guide_routes,
        language, login, onboarding as onboarding_routes,
        profile as profile_routes, report as report_routes, rivals_view,
        runstatus, settings, today, triage,
    )

    # First, so the sign-in pages exist before anything that needs a session.
    app.include_router(login.router)
    app.include_router(today.router)
    app.include_router(client.router)
    app.include_router(archive.router)
    app.include_router(settings.router)
    app.include_router(advisory.router)
    # The package on an impulse: the six formats, written, edited and released.
    # Its own module because everything on it acts on a stored text, while the
    # impulse page itself only renders one; the page reads the strip from
    # ``assets_view.package``.
    app.include_router(assets_view.router)
    app.include_router(assistant.router)
    app.include_router(language.router)
    app.include_router(triage.router)
    app.include_router(runstatus.router)
    app.include_router(guide_routes.router)
    app.include_router(contacts.router)
    app.include_router(profile_routes.router)
    app.include_router(rivals_view.router)
    app.include_router(report_routes.router)
    app.include_router(onboarding_routes.router)
    return app


def forwarded_allow_ips(host: str) -> str | None:
    """Which upstreams may set ``X-Forwarded-Proto``, given the bind address.

    uvicorn trusts those headers only from ``127.0.0.1`` by default. Behind a
    TLS-terminating proxy that is not loopback — Railway's router, or Caddy in
    front of the compose stack — the header is therefore ignored, the app
    believes it is serving plain http, and every absolute URL it generates comes
    out as ``http://``. On an https page the browser blocks those as mixed
    content, which takes out the stylesheet and htmx while leaving the HTML
    intact: the dashboard renders unstyled and inert rather than failing.

    A non-loopback bind means something is in front, because binding publicly
    without credentials is refused outright (see ``require_auth_for_public_bind``)
    and the deployment guide puts TLS in front in every route. So trust the
    proxy exactly then, and keep uvicorn's stricter default for local runs.

    Returning ``None`` leaves uvicorn's default in place rather than restating
    it, so this never silently pins a value uvicorn later changes.
    """
    return None if is_loopback(host) else "*"


def main() -> None:
    """Console entry point: start the dashboard with uvicorn (``newspulse-web``).

    uvicorn is imported here, not at module top, so importing this module for the
    route tests (which drive the app through Starlette's in-process TestClient)
    never requires an ASGI server to be installed.
    """
    import uvicorn

    from .. import config
    from . import scheduler

    # Before the socket opens, not after: a public bind with no credentials is
    # the one mistake that cannot be undone by noticing it later.
    require_auth_for_public_bind(config.WEB_HOST)
    # Here rather than in create_app(): the tests build the app hundreds of times
    # and none of them wants a thread that fetches feeds and shells out to a
    # model. The deployed platform starts exactly one process — this one — so
    # without this nothing ever runs the daily sweep.
    scheduler.start()
    uvicorn.run(
        create_app(),
        host=config.WEB_HOST,
        port=config.WEB_PORT,
        forwarded_allow_ips=forwarded_allow_ips(config.WEB_HOST),
    )


__all__ = ["create_app", "get_db", "logo_src", "main", "safe_url", "templates"]
