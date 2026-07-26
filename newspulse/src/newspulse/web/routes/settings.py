"""The Settings view: client CRUD, Excel/CSV import, feeds, threshold, run status.

One page (``/settings``) that is the operator's whole control surface:

* **Clients** — add, edit and (soft-)deactivate portfolio companies through the
  NP-02 CRUD service; deactivation never touches the client's archive.
* **Import** — an Excel/CSV import driven by an explicit column mapping, with a
  *preview* step (``preview_import``) that parses and validates the sheet and
  surfaces any NP-02 validation error inline **before** anything is written.
* **Threshold** — the alert importance threshold, persisted to the ``settings``
  table. It is the source of truth a *future* run reads, so changing it changes
  which future articles are flagged; it never rewrites stored analyses.
* **Feeds** — the registered feed list (``feeds_default.toml``) with a per-feed
  active toggle, the active set persisted to the ``settings`` table.
* **Runs** — the most recent ``runs`` rows (status, articles_found, errors) so the
  agent can see at a glance whether this morning's sweep succeeded.

Settings persistence lives here as small ``get_*``/``set_*`` helpers over the
key/value ``settings`` table; they are the read/write seam the daily job (NP-06)
uses for the effective alert threshold and active feeds.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import tempfile
from contextlib import contextmanager
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData
from starlette.datastructures import UploadFile as StarletteUploadFile

from ... import config
from ...clients import (
    ImportValidationError,
    create_client,
    deactivate_client,
    import_clients,
    list_clients,
    preview_import,
    update_client,
)
from ...feeds import load_feeds
from ...models import DEFAULT_COUNTRY, SCORE_MAX, SCORE_MIN, Run, Setting
from ..app import get_db, templates

router = APIRouter()

# --- Named constants (the "why" lives next to each) ----------------------------

# settings-table keys. Kept here so the read/write helpers and any future consumer
# name a setting by constant, never by a bare string spread across the package.
_ALERT_THRESHOLD_KEY = "alert_threshold"
_ACTIVE_FEEDS_KEY = "active_feeds"

# Recent runs shown in the history table — enough to eyeball a week-plus of daily
# sweeps without paging the whole (unbounded) runs history into one page.
_RUN_HISTORY_LIMIT = 10

# POST-Redirect-GET status: a successful mutation redirects so a browser refresh
# re-GETs the page instead of re-submitting the form.
_SEE_OTHER = 303

# A country is an ISO 3166-1 alpha-2 code (the model column is String(2)); the CRUD
# form enforces the width the same way the import does, since SQLite ignores it.
_COUNTRY_CODE_LEN = 2

# The client fields an import mapping may populate, in display order. Mirrors the
# importable set NP-02 accepts; ``name`` is the only required one.
_MAP_FIELDS: tuple[str, ...] = (
    "name",
    "aliases",
    "industry",
    "country",
    "keywords",
    "alert_topics",
)

# A delimited CRUD-form cell (aliases/keywords/alert_topics) splits on comma or
# semicolon — the same rule the sheet importer uses — so the agent types whichever.
_LIST_DELIMITER = re.compile(r"[;,]")


# --- settings-table service ----------------------------------------------------
#
# Small helpers over the key/value ``settings`` table. Writing a setting touches
# only that table, never any ``analyses`` row — which is exactly why changing the
# threshold affects *future* flagging without retroactively rewriting stored
# analyses.


def _upsert_setting(session: Session, key: str, value: str) -> None:
    """Insert or update one settings row in place (no duplicate keys)."""
    setting = session.get(Setting, key)
    if setting is None:
        session.add(Setting(key=key, value=value))
    else:
        setting.value = value


def get_alert_threshold(session: Session) -> int:
    """The effective alert threshold: the stored value, else the config default.

    This is the read seam the daily job uses to flag future articles; a missing or
    corrupt stored value falls back to ``config.ALERT_THRESHOLD`` so the sweep is
    never blocked by a bad settings row.
    """
    setting = session.get(Setting, _ALERT_THRESHOLD_KEY)
    if setting is None or setting.value is None:
        return config.ALERT_THRESHOLD
    try:
        return int(setting.value)
    except ValueError:
        return config.ALERT_THRESHOLD


def set_alert_threshold(session: Session, value: int) -> None:
    """Persist the alert threshold, clamped to the 0..10 importance scale."""
    clamped = max(SCORE_MIN, min(SCORE_MAX, value))
    _upsert_setting(session, _ALERT_THRESHOLD_KEY, str(clamped))
    session.commit()


def get_active_feed_names(session: Session, all_feeds: Iterable) -> set[str]:
    """The set of feed names currently active. Default (no stored value): all of
    the registered feeds, so a fresh install sweeps everything until narrowed."""
    setting = session.get(Setting, _ACTIVE_FEEDS_KEY)
    default = {feed.name for feed in all_feeds}
    if setting is None or setting.value is None:
        return default
    try:
        names = json.loads(setting.value)
    except json.JSONDecodeError:
        return default
    return set(names) if isinstance(names, list) else default


def set_active_feed_names(session: Session, names: Iterable[str]) -> None:
    """Persist the active feed set as a sorted JSON list (stable, diff-friendly)."""
    _upsert_setting(session, _ACTIVE_FEEDS_KEY, json.dumps(sorted(set(names))))
    session.commit()


# --- View models ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunView:
    """One row in the run-history table (times already in local tz)."""

    started_at: dt.datetime
    finished_at: dt.datetime | None
    status: str
    articles_found: int
    errors: list[str]

    @property
    def is_running(self) -> bool:
        return self.finished_at is None


@dataclass(frozen=True, slots=True)
class FeedView:
    """One registered feed plus whether it is currently active."""

    name: str
    url: str
    industry: str | None
    active: bool


@dataclass(frozen=True, slots=True)
class _HeaderRun:
    """The base-template header's last-run summary (base.html field contract)."""

    ran_at: dt.datetime
    is_running: bool
    status: str
    articles_checked: int
    feed_errors: int


def _fetch_runs(session: Session, limit: int) -> list[RunView]:
    """The most recent ``limit`` runs, newest first, times in the local zone."""
    runs = (
        session.execute(select(Run).order_by(Run.started_at.desc()).limit(limit))
        .scalars()
        .all()
    )
    return [
        RunView(
            # Stored UTC (UTCDateTime); shown local via astimezone() so the table
            # matches the header's last-run time.
            started_at=run.started_at.astimezone(),
            finished_at=run.finished_at.astimezone() if run.finished_at else None,
            status=run.status.value,
            articles_found=run.articles_found,
            errors=list(run.errors),
        )
        for run in runs
    ]


def _header_from_runs(runs: list[RunView]) -> _HeaderRun | None:
    """Derive the header last-run summary from the already-fetched run history."""
    if not runs:
        return None
    latest = runs[0]
    return _HeaderRun(
        ran_at=latest.finished_at or latest.started_at,
        is_running=latest.is_running,
        status=latest.status,
        articles_checked=latest.articles_found,
        feed_errors=len(latest.errors),
    )


def _fetch_feed_views(session: Session) -> list[FeedView]:
    """The registered feeds with their active flag resolved from settings."""
    feeds = load_feeds()
    active = get_active_feed_names(session, feeds)
    return [
        FeedView(name=f.name, url=f.url, industry=f.industry, active=f.name in active)
        for f in feeds
    ]


# --- Form parsing --------------------------------------------------------------


def _split_list(raw: str) -> list[str]:
    """Split a delimited CRUD-form cell into a clean list: trim, drop blanks."""
    return [part.strip() for part in _LIST_DELIMITER.split(raw or "") if part.strip()]


def _clean_country(raw: str) -> str:
    """Normalize a country field to an uppercase 2-letter ISO code (default DE).

    Raises ``ValueError`` (surfaced inline) on a non-ISO value so a full name like
    "Deutschland" can't silently overflow the ``String(2)`` column on SQLite.
    """
    code = (raw or "").strip().upper() or DEFAULT_COUNTRY
    if len(code) != _COUNTRY_CODE_LEN or not code.isalpha():
        raise ValueError(f"Land {raw!r} ist kein 2-Buchstaben-ISO-Code (z. B. 'DE').")
    return code


def _parse_client_form(
    *,
    name: str,
    aliases: str,
    industry: str,
    country: str,
    keywords: str,
    alert_topics: str,
) -> dict[str, object]:
    """Validate and shape a client CRUD form into service kwargs.

    Raises ``ValueError`` (with a German message) on an empty name or bad country,
    which the routes surface inline rather than 500ing.
    """
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Name ist erforderlich.")
    return {
        "name": clean_name,
        "aliases": _split_list(aliases),
        "industry": industry.strip() or None,
        "country": _clean_country(country),
        "keywords": _split_list(keywords),
        "alert_topics": _split_list(alert_topics),
    }


def _mapping_from_form(form: FormData) -> dict[str, str]:
    """Build the NP-02 ``{source_column: target_field}`` mapping from ``map_*``
    fields, dropping the ones left blank (an unmapped required ``name`` is then
    rejected by ``preview_import`` with a clear error)."""
    mapping: dict[str, str] = {}
    for field in _MAP_FIELDS:
        source = (form.get(f"map_{field}") or "").strip()
        if source:
            mapping[source] = field
    return mapping


def _invert_mapping(mapping: dict[str, str]) -> dict[str, str]:
    """{source: field} -> {field: source}, to re-fill the mapping inputs after a
    preview so the operator doesn't retype the mapping to commit."""
    return {field: source for source, field in mapping.items()}


@contextmanager
def _staged_file(filename: str, data: bytes) -> Iterator[Path]:
    """Write an uploaded blob to a temp file (keeping its suffix) for the path-based
    NP-02 importer, and delete it deterministically at scope exit.

    The suffix is preserved from the upload name so the importer's ``.xlsx``/``.csv``
    detection works; an unknown suffix flows through to a clear NP-02 error.
    """
    suffix = Path(filename).suffix
    fd, tmp_name = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_bytes(data)
        yield tmp
    finally:
        tmp.unlink(missing_ok=True)


async def _read_upload(form: FormData) -> tuple[str, bytes]:
    """Return ``(filename, data)`` for the form's ``file`` part, or ``("", b"")``
    when no file was actually selected (empty part with a blank filename)."""
    upload = form.get("file")
    if isinstance(upload, StarletteUploadFile) and upload.filename:
        return upload.filename, await upload.read()
    return "", b""


# --- Rendering -----------------------------------------------------------------


def _page_context(session: Session) -> dict[str, object]:
    """Shared context for every settings render (GET and the re-render paths).

    Template-facing extras (errors, preview rows, echoed mapping) default here so
    the template never touches an undefined variable; a route overrides them.
    """
    runs = _fetch_runs(session, _RUN_HISTORY_LIMIT)
    return {
        "clients": list_clients(session, include_inactive=True),
        "feeds": _fetch_feed_views(session),
        "runs": runs,
        "last_run": _header_from_runs(runs),
        "alert_threshold": get_alert_threshold(session),
        "score_range": list(range(SCORE_MIN, SCORE_MAX + 1)),
        "default_country": DEFAULT_COUNTRY,
        "map_fields": _MAP_FIELDS,
        "map_values": {},
        "client_error": None,
        "threshold_error": None,
        "import_error": None,
        "import_rows": None,
        "import_success": None,
    }


def _render_settings(
    request: Request, session: Session, **extra: object
) -> HTMLResponse:
    """Render the settings page with the shared context plus any per-route extras."""
    context = _page_context(session)
    context.update(extra)
    return templates.TemplateResponse(request, "settings.html", context)


# --- Routes --------------------------------------------------------------------


@router.get("/settings", response_class=HTMLResponse)
def settings_view(
    request: Request,
    imported: int | None = None,
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """Render the settings page. ``?imported=N`` shows a post-import success note."""
    extra: dict[str, object] = {}
    if imported is not None:
        extra["import_success"] = imported
    return _render_settings(request, session, **extra)


@router.post("/settings/clients")
def add_client_route(
    request: Request,
    name: str = Form(...),
    aliases: str = Form(""),
    industry: str = Form(""),
    country: str = Form(""),
    keywords: str = Form(""),
    alert_topics: str = Form(""),
    session: Session = Depends(get_db),
) -> Response:
    """Add a client through the NP-02 CRUD service."""
    try:
        fields = _parse_client_form(
            name=name,
            aliases=aliases,
            industry=industry,
            country=country,
            keywords=keywords,
            alert_topics=alert_topics,
        )
        create_client(session, **fields)
    except ValueError as exc:
        return _render_settings(request, session, client_error=str(exc))
    return RedirectResponse("/settings", status_code=_SEE_OTHER)


@router.post("/settings/clients/{client_id}")
def edit_client_route(
    client_id: int,
    request: Request,
    name: str = Form(...),
    aliases: str = Form(""),
    industry: str = Form(""),
    country: str = Form(""),
    keywords: str = Form(""),
    alert_topics: str = Form(""),
    session: Session = Depends(get_db),
) -> Response:
    """Edit an existing client through the NP-02 CRUD service."""
    try:
        fields = _parse_client_form(
            name=name,
            aliases=aliases,
            industry=industry,
            country=country,
            keywords=keywords,
            alert_topics=alert_topics,
        )
        update_client(session, client_id, **fields)
    except (ValueError, LookupError) as exc:
        return _render_settings(request, session, client_error=str(exc))
    return RedirectResponse("/settings", status_code=_SEE_OTHER)


@router.post("/settings/clients/{client_id}/deactivate")
def deactivate_client_route(
    client_id: int, session: Session = Depends(get_db)
) -> RedirectResponse:
    """Soft-deactivate a client. The archive (articles, analyses) stays intact."""
    try:
        deactivate_client(session, client_id)
    except LookupError:
        # Already gone / bad id: nothing to do, land back on the page.
        pass
    return RedirectResponse("/settings", status_code=_SEE_OTHER)


@router.post("/settings/clients/{client_id}/reactivate")
def reactivate_client_route(
    client_id: int, session: Session = Depends(get_db)
) -> RedirectResponse:
    """Reactivate a soft-deactivated client (an edit that flips ``active`` back)."""
    try:
        update_client(session, client_id, active=True)
    except LookupError:
        pass
    return RedirectResponse("/settings", status_code=_SEE_OTHER)


@router.post("/settings/threshold")
def set_threshold_route(
    request: Request,
    alert_threshold: str = Form(...),
    session: Session = Depends(get_db),
) -> Response:
    """Persist the alert threshold to the settings table (future flagging only)."""
    try:
        value = int(alert_threshold)
    except ValueError:
        return _render_settings(
            request, session, threshold_error="Schwellenwert muss eine Zahl sein."
        )
    set_alert_threshold(session, value)
    return RedirectResponse("/settings", status_code=_SEE_OTHER)


@router.post("/settings/feeds")
async def set_feeds_route(
    request: Request, session: Session = Depends(get_db)
) -> RedirectResponse:
    """Persist the active feed set (the checked ``feed`` boxes) to settings."""
    form = await request.form()
    set_active_feed_names(session, form.getlist("feed"))
    return RedirectResponse("/settings", status_code=_SEE_OTHER)


@router.post("/settings/import/preview", response_class=HTMLResponse)
async def import_preview_route(
    request: Request, session: Session = Depends(get_db)
) -> HTMLResponse:
    """Parse and validate the uploaded sheet, showing the rows (or an inline NP-02
    validation error) **without** writing anything."""
    form = await request.form()
    mapping = _mapping_from_form(form)
    filename, data = await _read_upload(form)
    if not data:
        return _render_settings(
            request, session, import_error="Bitte zuerst eine Datei auswählen."
        )
    try:
        with _staged_file(filename, data) as path:
            rows = preview_import(path, mapping)
    except ImportValidationError as exc:
        return _render_settings(
            request, session, import_error=str(exc), map_values=_invert_mapping(mapping)
        )
    return _render_settings(
        request, session, import_rows=rows, map_values=_invert_mapping(mapping)
    )


@router.post("/settings/import/commit")
async def import_commit_route(
    request: Request, session: Session = Depends(get_db)
) -> Response:
    """Commit the uploaded sheet through the NP-02 importer (create/update)."""
    form = await request.form()
    mapping = _mapping_from_form(form)
    filename, data = await _read_upload(form)
    if not data:
        return _render_settings(
            request, session, import_error="Bitte zuerst eine Datei auswählen."
        )
    try:
        with _staged_file(filename, data) as path:
            result = import_clients(path, mapping, session)
    except ImportValidationError as exc:
        return _render_settings(
            request, session, import_error=str(exc), map_values=_invert_mapping(mapping)
        )
    return RedirectResponse(
        f"/settings?imported={result.total}", status_code=_SEE_OTHER
    )


__all__ = [
    "router",
    "get_alert_threshold",
    "set_alert_threshold",
    "get_active_feed_names",
    "set_active_feed_names",
]
