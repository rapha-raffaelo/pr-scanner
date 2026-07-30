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
import logging
import os
import re
import tempfile
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData
from starlette.datastructures import UploadFile as StarletteUploadFile

from ... import config, job
from ...analyzer import get_analyzer
from ...db import get_session
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
from ...outlets import tier_for
from ...logos import fetch_logo, normalize_website
from ...models import DEFAULT_COUNTRY, SCORE_MAX, SCORE_MIN, Client, Run, Setting
from ..app import get_db, templates

router = APIRouter()

# --- Named constants (the "why" lives next to each) ----------------------------

# settings-table keys. Kept here so the read/write helpers and any future consumer
# name a setting by constant, never by a bare string spread across the package.
_ALERT_THRESHOLD_KEY = "alert_threshold"
# Feeds are persisted as the *deactivated* set (a deny-list), not the active set:
# storing which feeds the operator switched OFF means a feed later added to the
# registry is active by default — the operator never deselected it — instead of
# silently dropping off the moment the feed form was saved once.
_INACTIVE_FEEDS_KEY = "inactive_feeds"

# Recent runs shown in the history table — enough to eyeball a week-plus of daily
# sweeps without paging the whole (unbounded) runs history into one page.
_RUN_HISTORY_LIMIT = 10

# Rows rendered in the import preview. A large sheet parses fine, but rendering
# every row into one HTML table is unbounded; the preview only needs to let the
# operator eyeball that the mapping is right, so it is capped and the total noted.
_PREVIEW_ROW_LIMIT = 100

# POST-Redirect-GET status: a successful mutation redirects so a browser refresh
# re-GETs the page instead of re-submitting the form.
_SEE_OTHER = 303

_log = logging.getLogger(__name__)

# Backfill windows offered in the dashboard. A sweep only widens which *fetched*
# items are accepted — a feed still returns only what it currently syndicates —
# so these are best-effort catch-up windows, not a guarantee of N days of history.
BACKFILL_DAYS = (10, 15, 30)

# One sweep at a time. A run fetches 40+ feeds and shells out to `claude` per
# batch; two concurrent runs would double-fetch and race on the same articles.
# Non-blocking acquire, so a second click is refused immediately rather than
# queueing another full sweep behind the first.
_run_guard = threading.Lock()


def _execute_run(since_days: int | None) -> None:
    """Run one sweep on a worker thread; always release the guard.

    A sweep takes minutes (40+ feed fetches plus a `claude` call per batch), far
    longer than a request should hold a connection open, so the route starts this
    and returns immediately. It opens its own session: the request-scoped one is
    closed the moment the response is sent.
    """
    try:
        job.setup_logging()
        since = job.lookback_since(since_days) if since_days is not None else None
        with get_session() as session:
            report = job.run(session, analyzer=get_analyzer(), since=since)
        _log.info(
            "dashboard-triggered run finished: status=%s new_articles=%d",
            report.status.value,
            report.new_articles,
        )
    except Exception:  # noqa: BLE001 — a worker thread must never die silently
        _log.exception("dashboard-triggered run failed before it could report")
    finally:
        _run_guard.release()

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
    """The set of feed names currently active: every registered feed minus the ones
    the operator has explicitly deactivated.

    Storing the *deactivated* set (see ``_INACTIVE_FEEDS_KEY``) means the default —
    no stored value, or a corrupt one — is "all feeds active", and a feed added to
    the registry after the last save is active by default rather than silently off.
    """
    all_names = {feed.name for feed in all_feeds}
    setting = session.get(Setting, _INACTIVE_FEEDS_KEY)
    if setting is None or setting.value is None:
        return all_names
    try:
        inactive = json.loads(setting.value)
    except json.JSONDecodeError:
        return all_names
    if not isinstance(inactive, list):
        return all_names
    return all_names - set(inactive)


def set_active_feed_names(
    session: Session, active_names: Iterable[str], all_names: Iterable[str]
) -> None:
    """Persist which feeds are active by storing their *complement* — the feeds the
    operator switched off — as a sorted JSON list (stable, diff-friendly).

    ``active_names`` is intersected with ``all_names`` first so a stale name from an
    old form can't linger, and the stored deny-list is exactly the registered feeds
    the operator left unchecked.
    """
    known = set(all_names)
    inactive = sorted(known - (set(active_names) & known))
    _upsert_setting(session, _INACTIVE_FEEDS_KEY, json.dumps(inactive))
    session.commit()


# --- View models ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunView:
    """One row in the run-history table (times stored UTC, rendered local)."""

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
    """The most recent ``limit`` runs, newest first, times as stored (UTC)."""
    runs = (
        session.execute(select(Run).order_by(Run.started_at.desc()).limit(limit))
        .scalars()
        .all()
    )
    return [
        RunView(
            # Left in UTC: the template's de_datetime/de_time filters convert to
            # the reader's zone, so the conversion lives in exactly one place
            # (see web.app._local) instead of once per view that renders a time.
            started_at=run.started_at,
            finished_at=run.finished_at,
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
    rejected by ``preview_import`` with a clear error).

    Raises ``ImportValidationError`` if the operator points two fields at the same
    source column: the mapping is keyed by source, so the second would silently
    overwrite the first (e.g. dropping ``name``) and mis-report it as "not mapped".
    """
    mapping: dict[str, str] = {}
    for field in _MAP_FIELDS:
        source = (form.get(f"map_{field}") or "").strip()
        if not source:
            continue
        if source in mapping:
            raise ImportValidationError(
                f"Column {source!r} is mapped to two fields "
                f"({mapping[source]!r} and {field!r}); map each column to one field"
            )
        mapping[source] = field
    return mapping


def _echoed_map_values(form: FormData) -> dict[str, str]:
    """The operator's raw ``{field: source}`` entries read straight from the form,
    to re-fill the mapping inputs after any error so the mapping needn't be retyped.

    Read from the form (not from a successfully-built mapping) so the echo survives
    a duplicate-source error, where no valid mapping exists to invert.
    """
    return {
        field: source
        for field in _MAP_FIELDS
        if (source := (form.get(f"map_{field}") or "").strip())
    }


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
        # Grouped by tier: 58 checkboxes in one block is a wall, and the
        # question an operator actually has is "are the Leitmedien on?" — which
        # a flat list cannot answer at a glance.
        "feed_tiers": _feeds_by_tier(_fetch_feed_views(session)),
        "runs": runs,
        "last_run": _header_from_runs(runs),
        # The shared header dates every page; this view has no viewed-day of its
        # own, so it shows today — in the reader's zone, like every other page.
        "header_date": dt.datetime.now(config.local_zone()).date(),
        "backfill_days": BACKFILL_DAYS,
        "run_error": None,
        "run_started": None,
        "alert_threshold": get_alert_threshold(session),
        # Whether a backup provider is armed. Shown rather than assumed: a
        # fallback nobody can see is one nobody knows has stopped working, and
        # its absence only becomes visible on the morning the subscription runs
        # out — which is the morning it mattered.
        "fallback_ready": config.gemini_configured(),
        "fallback_model": config.GEMINI_MODEL,
        "score_range": list(range(SCORE_MIN, SCORE_MAX + 1)),
        "default_country": DEFAULT_COUNTRY,
        "map_fields": _MAP_FIELDS,
        "map_values": {},
        "client_error": None,
        "threshold_error": None,
        "import_error": None,
        "import_rows": None,
        "import_row_total": None,
        "import_success": None,
    }


# Tier labels for the feed panel, in reading order. Same tiers the archive
# filter uses, so "Tier 1" means one thing across the app.
_FEED_TIERS = (
    (1, "Tier 1 — Leitmedien"),
    (2, "Tier 2 — Fach- & Regionalpresse"),
    (3, "Tier 3 — Finanz-Ticker"),
)


def _feeds_by_tier(feeds) -> list[tuple[str, list]]:
    """``[(label, feeds)]`` for the feed panel, empty tiers omitted."""
    buckets: dict[int, list] = {tier: [] for tier, _ in _FEED_TIERS}
    for feed in feeds:
        buckets.setdefault(tier_for(feed.name), []).append(feed)
    return [
        (label, buckets[tier]) for tier, label in _FEED_TIERS if buckets.get(tier)
    ]


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
    edit: int | None = None,
    started: int | None = None,
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """Render the settings page. ``?imported=N`` shows a post-import success note;
    ``?edit=<client_id>`` opens that one client's row as an edit form.

    Editing is opt-in per row so the portfolio reads as a scannable table: a
    30-client portfolio is 30 rows to skim, not 180 always-open input boxes.
    Which row is open lives in the URL, so it survives a reload and the
    validation-error re-render below without any client-side state.
    """
    extra: dict[str, object] = {}
    if imported is not None:
        extra["import_success"] = imported
    if edit is not None:
        extra["edit_id"] = edit
    if started is not None:
        extra["run_started"] = started
    return _render_settings(request, session, **extra)


@router.post("/settings/run")
def trigger_run_route(
    request: Request,
    since_days: str = Form(""),
    redirect_to: str = Form(""),
    session: Session = Depends(get_db),
) -> Response:
    """Start a sweep from the dashboard — the only way to fetch news without a
    terminal. ``since_days`` empty means the normal "since the last run" window.
    """
    raw = (since_days or "").strip()
    days: int | None = None
    if raw:
        if not raw.isdigit() or int(raw) not in BACKFILL_DAYS:
            return _render_settings(
                request, session, run_error=f"Ungültiger Zeitraum: {raw!r}"
            )
        days = int(raw)

    # Non-blocking: a run already in flight is reported, not queued.
    if not _run_guard.acquire(blocking=False):
        return _render_settings(
            request, session, run_error="Es läuft bereits ein Lauf."
        )
    # daemon=True: a local single-user tool should not refuse to shut down because
    # a sweep is mid-flight. An interrupted run leaves its `runs` row unfinished,
    # which the header already renders as "Lauf läuft…".
    threading.Thread(
        target=_execute_run, args=(days,), daemon=True, name="newspulse-run"
    ).start()
    # Return to where the click came from — the header button is on every page,
    # and bouncing the reader to Settings would lose their place. Only ever a
    # same-app path: the value comes from a form field, so an absolute URL here
    # would make this an open redirect.
    if redirect_to.startswith("/"):
        return RedirectResponse(redirect_to, status_code=_SEE_OTHER)
    return RedirectResponse(f"/settings?started={days or 0}", status_code=_SEE_OTHER)


@router.post("/settings/clients/{client_id}/competitor")
def toggle_competitor_route(
    client_id: int, session: Session = Depends(get_db)
) -> RedirectResponse:
    """Flip a client between mandate and competitor.

    A competitor is monitored identically — matched, analysed, archived — but is
    excluded from the digest and reported *about* rather than *to*. Toggling is
    non-destructive: the archive is untouched either way.
    """
    client = session.get(Client, client_id)
    if client is not None:
        client.is_competitor = not client.is_competitor
        session.commit()
    return RedirectResponse("/settings", status_code=_SEE_OTHER)


@router.post("/settings/clients/{client_id}/logo")
def fetch_logo_route(
    client_id: int, session: Session = Depends(get_db)
) -> RedirectResponse:
    """Fetch this client's logo from its own website and store it locally.

    Runs inline rather than on a thread: it is a single small request with a
    short timeout, and the operator clicked it expecting a result on the next
    page. A failure is silent-but-logged — a missing logo is cosmetic, and the
    monogram already stands in.
    """
    client = session.get(Client, client_id)
    if client is not None and client.website:
        logo = fetch_logo(client.website)
        if logo:
            client.logo_url = logo
            session.commit()
        else:
            _log.info("no usable logo found at %s", client.website)
    return RedirectResponse("/settings", status_code=_SEE_OTHER)


@router.post("/client/{client_id}/competitors")
def add_competitor_route(
    client_id: int, competitor_id: str = Form(...), session: Session = Depends(get_db)
) -> RedirectResponse:
    """Add a company to this client's comparison set.

    One-directional on purpose: benchmarking a mandate against a market leader
    should not put the mandate into the leader's own comparison.
    """
    client = session.get(Client, client_id)
    raw = (competitor_id or "").strip()
    if client is not None and raw.isdigit():
        other = session.get(Client, int(raw))
        # The schema forbids a self-link; refuse it here too so the operator
        # gets a no-op rather than a 500 from the CHECK constraint.
        if other is not None and other.id != client.id and other not in client.competitors:
            client.competitors.append(other)
            session.commit()
    return RedirectResponse(f"/client/{client_id}", status_code=_SEE_OTHER)


@router.post("/client/{client_id}/competitors/{competitor_id}/remove")
def remove_competitor_route(
    client_id: int, competitor_id: int, session: Session = Depends(get_db)
) -> RedirectResponse:
    """Remove a company from this client's comparison set. The company itself and
    its archive are untouched — only the link is dropped."""
    client = session.get(Client, client_id)
    if client is not None:
        other = session.get(Client, competitor_id)
        if other is not None and other in client.competitors:
            client.competitors.remove(other)
            session.commit()
    return RedirectResponse(f"/client/{client_id}", status_code=_SEE_OTHER)


@router.post("/settings/clients")
def add_client_route(
    request: Request,
    name: str = Form(...),
    aliases: str = Form(""),
    industry: str = Form(""),
    country: str = Form(""),
    keywords: str = Form(""),
    alert_topics: str = Form(""),
    website: str = Form(""),
    session: Session = Depends(get_db),
) -> Response:
    """Add a client through the NP-02 CRUD service, with its logo if reachable."""
    try:
        fields = _parse_client_form(
            name=name,
            aliases=aliases,
            industry=industry,
            country=country,
            keywords=keywords,
            alert_topics=alert_topics,
        )
        created = create_client(session, **fields)
        site = normalize_website(website)
        if site:
            created.website = site
            # Best-effort and inline: a new client should look finished
            # immediately, and the monogram covers the case where it fails.
            created.logo_url = fetch_logo(site)
            session.commit()
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
        # Keep this row open so the rejected edit is still on screen to correct,
        # rather than collapsing back to the read-only row and losing it.
        return _render_settings(
            request, session, client_error=str(exc), edit_id=client_id
        )
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
    client_id: int, request: Request, session: Session = Depends(get_db)
) -> Response:
    """Reactivate a soft-deactivated client (an edit that flips ``active`` back).

    Reactivation can violate the "one active client per name" invariant if another
    active client already holds this name; ``update_client`` rejects that and the
    conflict is surfaced inline rather than silently creating a duplicate.
    """
    try:
        update_client(session, client_id, active=True)
    except LookupError:
        # Already gone / bad id: nothing to do, land back on the page.
        return RedirectResponse("/settings", status_code=_SEE_OTHER)
    except ValueError as exc:
        return _render_settings(request, session, client_error=str(exc))
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
    """Persist the active feed set (the checked ``feed`` boxes) to settings.

    The full registry is passed so the stored deny-list is the exact complement of
    what the operator checked, keeping newly-added feeds active by default.
    """
    form = await request.form()
    all_names = [feed.name for feed in load_feeds()]
    set_active_feed_names(session, form.getlist("feed"), all_names)
    return RedirectResponse("/settings", status_code=_SEE_OTHER)


@router.post("/settings/import/preview", response_class=HTMLResponse)
async def import_preview_route(
    request: Request, session: Session = Depends(get_db)
) -> HTMLResponse:
    """Parse and validate the uploaded sheet, showing the rows (or an inline NP-02
    validation error) **without** writing anything."""
    form = await request.form()
    filename, data = await _read_upload(form)
    map_values = _echoed_map_values(form)
    if not data:
        return _render_settings(
            request,
            session,
            import_error="Bitte zuerst eine Datei auswählen.",
            map_values=map_values,
        )
    try:
        mapping = _mapping_from_form(form)
        with _staged_file(filename, data) as path:
            rows = preview_import(path, mapping)
    except ImportValidationError as exc:
        return _render_settings(
            request, session, import_error=str(exc), map_values=map_values
        )
    return _render_settings(
        request,
        session,
        import_rows=rows[:_PREVIEW_ROW_LIMIT],
        import_row_total=len(rows),
        map_values=map_values,
    )


@router.post("/settings/import/commit")
async def import_commit_route(
    request: Request, session: Session = Depends(get_db)
) -> Response:
    """Commit the uploaded sheet through the NP-02 importer (create/update)."""
    form = await request.form()
    filename, data = await _read_upload(form)
    map_values = _echoed_map_values(form)
    if not data:
        return _render_settings(
            request,
            session,
            import_error="Bitte zuerst eine Datei auswählen.",
            map_values=map_values,
        )
    try:
        mapping = _mapping_from_form(form)
        with _staged_file(filename, data) as path:
            result = import_clients(path, mapping, session)
    except ImportValidationError as exc:
        return _render_settings(
            request, session, import_error=str(exc), map_values=map_values
        )
    return RedirectResponse(
        f"/settings?imported={result.total}", status_code=_SEE_OTHER
    )


__all__ = [
    "get_active_feed_names",
    "get_alert_threshold",
    "router",
    "set_active_feed_names",
    "set_alert_threshold",
]
