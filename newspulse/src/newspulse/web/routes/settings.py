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

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData
from starlette.datastructures import UploadFile as StarletteUploadFile

from ... import (
    angles,
    brain,
    config,
    industry,
    job,
    outreach,
    pitch,
    radar_cleanup,
    rivals,
    themes,
)
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
from ...models import (
    DEFAULT_COUNTRY,
    SCORE_MAX,
    SCORE_MIN,
    Category,
    Client,
    Run,
    Setting,
)
from .. import runlock, themework
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
#
# 90 is the onboarding window. Adding a mandate is the one moment the archive is
# supposed to fill *backwards*, and a Google News search still lists a company's
# coverage for months: a newly added client's launch coverage was 79 days old, so
# every window on offer returned nothing and the mandate looked unmonitored. The
# upper bound stays a fixed choice rather than a free-text field because a sweep
# analyses everything it accepts, and a year would spend the subscription on
# coverage nobody asked to re-read.
BACKFILL_DAYS = (10, 15, 30, 90)

# One sweep at a time (see web.runlock for why the lock lives there now: the
# layout needs the same signal to draw the header spinner, and it cannot import a
# route to get it). Re-exported under the old name so the tests and call sites
# that reach for ``settings._run_guard`` keep working.
_run_guard = runlock.guard

# The categories a mandate may mute, as the analyzer stores them. Read off the
# enum so a new category cannot quietly become unmutable.
_CATEGORY_VALUES = tuple(c.value for c in Category)


# Which mandates are still being set up, and what happened to the ones that
# finished. Onboarding runs on a worker thread and takes minutes; the only signal
# was the global "Läuft…" in the header, which does not distinguish "the nightly
# sweep is running" from "the client I just created is being filled in". A person
# who has just submitted a form and sees nothing about it concludes the form did
# nothing. In memory rather than a schema change: it describes one background
# job, and forgetting it on restart is correct.
_onboarding: dict[int, str] = {}


def _onboard(client_id: int, name: str) -> None:
    """Fetch a newly created client's recent coverage on a worker thread.

    Waits for the guard rather than giving up on it: a mandate created while the
    daily sweep is running should still arrive with coverage, just a few minutes
    later. Blocking is safe here — this is a daemon thread, and the only thing
    waiting on it is the archive filling in.

    The industry is settled first, because everything after it depends on the
    field: the topic radar scopes its query with it, and the archive linking
    refuses to run without it. A mandate created with the field blank would
    otherwise be onboarded into exactly the state that produces no market
    material and no impulses.
    """
    _onboarding[client_id] = "wartet"
    try:
        with runlock.guard:
            _onboarding[client_id] = "läuft"
            with get_session() as session:
                client = session.get(Client, client_id)
                if client is None:  # deleted between creation and this thread
                    _onboarding.pop(client_id, None)
                    return
                _settle_industry(session, client)
                themes.settle(session, client)
                stored = job.backfill_client(session, client)
                _log.info("onboarding fetch for %r stored %d article(s)", name, stored)
                _onboarding[client_id] = "entwürfe"
                _first_drafts(session, client)
                _onboarding[client_id] = f"fertig:{stored}"
    except Exception as exc:  # noqa: BLE001 — a worker thread must never die silently
        # A failed setup must not read as "this mandate simply has no press".
        _onboarding[client_id] = f"fehler:{exc}"
        _log.exception("onboarding fetch for %r failed", name)


def _settle_industry(session: Session, client: Client) -> None:
    """Give a new mandate a searchable industry if it arrived without one.

    Measured, not guessed: the term is a filter, and one the press does not write
    filters everything away. Failures are logged and swallowed — a mandate must
    still be onboarded if the classifier is unavailable.
    """
    if (client.industry or "").strip():
        return
    try:
        best = industry.classify(client)
    except Exception as exc:  # noqa: BLE001 — onboarding must not depend on it
        _log.warning("industry classification for %r failed: %s", client.name, exc)
        return
    if best is None:
        _log.info("no usable industry term found for %r", client.name)
        return
    update_client(session, client.id, industry=best.term)
    _log.info(
        "classified %r as %r (%d item(s) of press use the word)",
        client.name, best.term, best.hits,
    )


def _first_drafts(session: Session, client: Client) -> None:
    """Give a brand-new mandate a position and a message to send it as.

    "Sobald ein Mandant angelegt ist, sollte eigentlich immer ein Impuls und eine
    Empfehlung platziert sein — das sollte nie leer sein."

    Until now the page was empty until the next nightly sweep, which is a poor
    first impression of a tool whose whole promise is "here is what to say". The
    steps run in the order their inputs arrive: the archive is linked to the
    client's themes first (the backfill has just stored coverage and the industry
    is settled, so there is finally something to match against), then the
    positioning from that market material, then the letter that carries it to the
    first recipient the pitch list can name.

    That last step used to be a separate "recommendation" read off the client's
    own press. It is the same material — the coverage is what makes a stranger's
    pitch credible — doing a job instead of describing one.

    Attempted, not guaranteed. With no market material the model has nothing to
    position against and says so, and manufacturing an opening to fill the panel
    is the one thing this feature must never do. Each step is isolated: a mandate
    must still arrive if one of them fails.
    """
    now = dt.datetime.now(dt.UTC)
    try:
        linked = job.link_archive_to_themes(
            session, [client], now - job.IMPULSE_LOOKBACK, now
        )
        _log.info("onboarding linked %d archived article(s) for %r", linked, client.name)
    except Exception:  # noqa: BLE001 — one step must not take the others with it
        _log.exception("archive linking during onboarding failed for %r", client.name)

    try:
        job._refresh_impulses(session, [client], [], now=now)
    except Exception:  # noqa: BLE001
        _log.exception("first impulse for %r failed", client.name)

    try:
        # Only if there is a position to carry. Without an impulse there is
        # nothing to personalise, and a letter with no thesis in it is a form.
        first = angles.latest(session, client.id)
        if first is not None:
            targets = pitch.targets_for(session, client, first)
            target = targets[0] if targets else None
            message = outreach.draft(session, client, first, target)
            review = reviewed_by = None
            try:
                review, reviewed_by = outreach.crosscheck(
                    session, client, first, message, target
                )
            except Exception as exc:  # noqa: BLE001 — no key, no network, no loss
                _log.info("first message for %r not cross-checked: %s", client.name, exc)
            outreach.store(
                session, client, first, message, target,
                review=review, reviewed_by=reviewed_by or "",
            )
            _log.info("first message for %r written", client.name)
    except Exception:  # noqa: BLE001
        _log.exception("first message for %r failed", client.name)


def _start_onboarding(client_id: int, name: str) -> None:
    """Kick off the onboarding fetch without holding up the form response.

    A new mandate is empty until something is fetched for it, and fetching plus
    analysing takes minutes — far too long to leave a form submission hanging. The
    header's spinner covers the wait, because this holds the same guard a sweep
    does.
    """
    threading.Thread(
        target=_onboard,
        args=(client_id, name),
        daemon=True,
        name=f"newspulse-onboard-{client_id}",
    ).start()


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


@dataclass(frozen=True, slots=True)
class BlockView:
    """One brain block as the settings panel shows it.

    ``text`` is what a prompt composes today, whichever source it came from, so
    the panel cannot show one thing while the model reads another.
    """

    key: str
    text: str
    is_override: bool
    #: An override whose shipped default is gone — a block renamed in the
    #: repository while an override for the old key was still live. Shown rather
    #: than hidden: it is the one state in which an edit nobody can find is still
    #: in force.
    is_orphan: bool
    changed_at: dt.datetime | None
    changed_by: str
    #: Whether ``changed_by`` is the sentinel rather than a person's name, so the
    #: template knows which of the two to put through the translation lookup.
    #: See :func:`_author_is_sentinel`.
    changed_by_is_sentinel: bool
    #: The brain version this text has been in force since, or None if the block
    #: has never been changed and is simply what the repository ships.
    version: int | None


@dataclass(frozen=True, slots=True)
class BlockChange:
    """One entry in a block's history, with the wording it put in force."""

    version: int
    changed_at: dt.datetime
    changed_by: str
    changed_by_is_sentinel: bool
    #: The wording this change put in force, or "" for a revert whose shipped
    #: default no longer exists — the one case where there is nothing to show and
    #: the template says so rather than rendering an empty box.
    text: str
    is_revert: bool


def _author_is_sentinel(name: str) -> bool:
    """Whether an author is the no-named-user fallback rather than a person.

    The panel used to render every author through the translation lookup, which
    works for "mensch" — that is why the entry exists — and is wrong for
    everything else. ``NEWSPULSE_AUTH_USER`` is a value an operator chooses, and
    one that happened to collide with a German UI key would show the author of a
    change as an unrelated English word: a user named "Vorgabe" appearing in the
    history as "Shipped". Only the sentinel is chrome.
    """
    return name == brain._ANONYMOUS_EDITOR


def _brain_blocks(session: Session) -> list[BlockView]:
    """Every block the tool has: what it says, where it came from, when it moved."""
    shipped = brain.shipped()
    overrides = brain.stored(session)
    changes = brain.latest(session)
    views: list[BlockView] = []
    for key in [*sorted(shipped), *brain.orphaned(overrides)]:
        change = changes.get(key)
        author = change.edited_by if change is not None else ""
        views.append(
            BlockView(
                key=key,
                text=overrides[key] if key in overrides else shipped.get(key, ""),
                is_override=key in overrides,
                is_orphan=key not in shipped,
                changed_at=change.edited_at if change is not None else None,
                changed_by=author,
                changed_by_is_sentinel=_author_is_sentinel(author),
                version=change.version if change is not None else None,
            )
        )
    return views


def _brain_history(session: Session, key: str) -> list[BlockChange]:
    """One block's recorded changes, newest first, each with its own wording.

    A revert carries no text of its own — it is the absence of an override — so
    it renders the shipped wording it restored. A date alone would say a change
    happened and nothing about what the house believed afterwards, which is the
    only question anyone opens a history to answer.

    Two honest limits. The shipped text is today's, not the one that shipped on
    the day of the revert: the file's wording at that moment is not stored
    anywhere this can read, and git holds the lineage. And for an orphan there is
    no shipped text at all, which used to render as an empty ``<pre>`` — a
    version, a date and an author with nothing readable beside them. That case is
    now empty on purpose and the template names it.
    """
    shipped_text = brain.shipped().get(key, "")
    return [
        BlockChange(
            version=row.version,
            changed_at=row.edited_at,
            changed_by=row.edited_by,
            changed_by_is_sentinel=_author_is_sentinel(row.edited_by),
            text=row.text if row.text is not None else shipped_text,
            is_revert=row.text is None,
        )
        for row in brain.history(session, key)
    ]


def _require_block(session: Session, key: str) -> None:
    """404 for a key that neither ships nor has an override.

    A block is addressed by name in the URL, so without this a typo renders the
    whole settings page with an editor for a block that does not exist and a save
    button that would refuse it.
    """
    if key not in brain.shipped() and key not in brain.stored(session):
        raise HTTPException(status_code=404, detail="Brain block not found")


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


# The country names an operator actually types instead of the code, in both
# interface languages. Rejecting "Deutschland" is technically correct and
# practically silly: the field wants a fact, the person supplied it, and only the
# encoding differed. Deliberately short — the editions the tool supports plus the
# neighbours it is likeliest to be asked for, not a world atlas whose long tail
# would be maintained by nobody.
_COUNTRY_NAMES = {
    "deutschland": "DE", "germany": "DE",
    "österreich": "AT", "oesterreich": "AT", "austria": "AT",
    "schweiz": "CH", "switzerland": "CH", "suisse": "CH",
    "frankreich": "FR", "france": "FR",
    "italien": "IT", "italy": "IT",
    "niederlande": "NL", "netherlands": "NL",
    "spanien": "ES", "spain": "ES",
    "polen": "PL", "poland": "PL",
    "vereinigtes königreich": "GB", "grossbritannien": "GB",
    "großbritannien": "GB", "united kingdom": "GB", "england": "GB",
    "usa": "US", "vereinigte staaten": "US", "united states": "US",
}


def _clean_country(raw: str) -> str:
    """Normalize a country field to an uppercase 2-letter ISO code (default DE).

    Accepts the country's name as well as its code: typing "Deutschland" into a
    field labelled "Land" is the obvious thing to do, and being told after
    submitting that it is "kein 2-Buchstaben-ISO-Code" is a rule the form never
    stated. Anything still unrecognised raises ``ValueError`` (surfaced inline)
    so a full name cannot silently overflow the ``String(2)`` column on SQLite.
    """
    value = (raw or "").strip()
    if not value:
        return DEFAULT_COUNTRY
    named = _COUNTRY_NAMES.get(value.casefold())
    if named:
        return named
    code = value.upper()
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


@router.post("/settings/radar/cleanup")
def clean_radar_route(
    hit: list[str] = Form(default_factory=list),
    session: Session = Depends(get_db),
) -> RedirectResponse:
    """Remove exactly the links the operator was shown.

    The rows travel with the form as ``client_id:article_id`` rather than being
    re-derived here. Re-deriving them meant the page could render forty rows and
    the button delete eight hundred, and that a hit added by the night's sweep
    between reading and pressing was deleted unseen.
    """
    pairs: list[tuple[int, int]] = []
    for raw in hit:
        client_id, _, article_id = (raw or "").partition(":")
        if client_id.isdigit() and article_id.isdigit():
            pairs.append((int(client_id), int(article_id)))
    removed = radar_cleanup.remove(session, pairs)
    _log.info("radar cleanup removed %d hit(s) on request", removed)
    return RedirectResponse("/settings?radar=1", status_code=_SEE_OTHER)


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
        # None means "not asked for". The one route that offers the survey
        # overrides it; every other render leaves the panel as an invitation.
        "radar_stale": None,
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
        # Per-client setup status, so a mandate created a minute ago says so on
        # its own row instead of leaving the reader to infer it from a global
        # spinner that means several different things.
        "onboarding": dict(_onboarding),
        # The theme proposal for whichever client was last asked about, with the
        # measurement behind each one.
        "themes": dict(themework.state),
        "rival_work": dict(themework.rivals_job.state),
        "industry_work": dict(themework.industry_job.state),
        # Offered as mutable categories in the edit form.
        "categories": _CATEGORY_VALUES,
        # What the house believes, block by block, and how often it has moved.
        # Read on every settings render rather than cached: this is the page the
        # edit lands on, and a panel that shows the previous wording after a save
        # is worse than one that shows nothing.
        "brain_blocks": _brain_blocks(session),
        "brain_version": brain.version(session),
        # Which block is open for editing, its history, and any refused edit.
        # None means "the panel is a list", which is every render but the one
        # reached through /settings/brain/<key>.
        "brain_open": None,
        "brain_history": [],
        "brain_error": None,
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
    radar: int | None = None,
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """Render the settings page. ``?imported=N`` shows a post-import success note;
    ``?edit=<client_id>`` opens that one client's row as an edit form;
    ``?radar=1`` runs the radar survey, which walks every stored hit and is
    therefore not something to do on a page this often opened.

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
    if radar:
        extra["radar_stale"] = radar_cleanup.survey(session)
    return _render_settings(request, session, **extra)


# --- The brain: what the house believes, and who last changed it ---------------
#
# The standards live in the repository and are overridden here, which is the
# whole of DEC-1 option C: a fresh install thinks correctly on day one, git keeps
# the lineage, and a consultant does not need a deployment to change a sentence
# about tone. Every block is its own form on purpose. A change to tonality is a
# different act from a change to what counts as evidence, and one textarea
# holding everything would make them the same act.


@router.get("/settings/brain/{key}", response_class=HTMLResponse)
def brain_block_view(
    key: str, request: Request, session: Session = Depends(get_db)
) -> HTMLResponse:
    """The settings page with one block open for editing, and its history.

    A URL of its own rather than a panel that only opens on click, because a
    version has to be citable: BRN-03 stamps every generated text with the brain
    version it was written under, and that stamp is only worth something if it
    links to the wording it names.
    """
    _require_block(session, key)
    return _render_settings(
        request, session, brain_open=key, brain_history=_brain_history(session, key)
    )


@router.post("/settings/brain/{key}")
def edit_brain_block_route(
    key: str,
    request: Request,
    text: str = Form(...),
    session: Session = Depends(get_db),
) -> Response:
    """Store an override for one block; it governs the next generated text.

    A refused edit re-renders with the block still open and the error above it,
    rather than redirecting to a page that says nothing about what happened. The
    box comes back holding the wording still in force, because the only edit this
    refuses is an empty one and there is nothing in it worth echoing.

    Guarded by ``_require_block`` like the other two verbs, rather than leaning on
    ``brain.edit`` to raise: one function deciding what exists is what keeps a GET
    and a POST for the same URL from giving different answers.
    """
    _require_block(session, key)
    try:
        brain.edit(session, key, text)
    except brain.UnknownBlock:
        raise HTTPException(status_code=404, detail="Brain block not found") from None
    except ValueError as exc:
        return _render_settings(
            request,
            session,
            brain_open=key,
            brain_history=_brain_history(session, key),
            brain_error=str(exc),
        )
    return RedirectResponse(f"/settings/brain/{key}", status_code=_SEE_OTHER)


@router.post("/settings/brain/{key}/revert")
def revert_brain_block_route(
    key: str, session: Session = Depends(get_db)
) -> RedirectResponse:
    """Put the shipped wording back, as its own recorded change.

    Lands back on the block unless the revert was the last thing keeping an
    orphan alive, in which case there is no block left to land on.
    """
    _require_block(session, key)
    brain.revert(session, key)
    back = f"/settings/brain/{key}" if key in brain.shipped() else "/settings#brain"
    return RedirectResponse(back, status_code=_SEE_OTHER)


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


# --- Theme suggestions ----------------------------------------------------------
# What a mandate's radar searches decides whether it ever has anything to say, and
# the terms an operator types describe the company rather than its field: a
# beauty-tech mandate carried "KI in der Kosmetik", which reads perfectly and
# returns nothing, because no journalist writes that phrase. Proposing themes is
# only half the answer — a proposal reads plausible whether or not the press
# covers it — so each one is put through the real radar query before it is offered,
# and the operator sees what it actually returns.
#
# The work is a model call plus one feed fetch per proposal, which is far too long
# to hold a form submission open. It runs on a worker thread and the page polls,
# the same shape the impulse uses.
@router.post("/settings/clients/{client_id}/themes")
def suggest_themes_route(
    client_id: int, session: Session = Depends(get_db)
) -> RedirectResponse:
    """Start the theme proposal for one client and return to the page."""
    themework.start(session, client_id)
    return RedirectResponse("/settings", status_code=_SEE_OTHER)


@router.post("/settings/clients/{client_id}/themes/accept")
def accept_theme_route(
    client_id: int,
    term: str = Form(...),
    redirect_to: str = Form(""),
    session: Session = Depends(get_db),
) -> RedirectResponse:
    """Add one proposed theme to the client's search terms and search it now.

    Immediately rather than at the next nightly sweep: the person just decided
    this theme is worth watching, and making them wait a day to find out whether
    it brought anything back is how a configuration screen stops being trusted.
    """
    # Same-site paths only: the value comes from a form field, and a redirect that
    # accepts anything is an open redirect.
    back = redirect_to if redirect_to.startswith("/") and "//" not in redirect_to else "/settings"
    client = session.get(Client, client_id)
    chosen = (term or "").strip()
    if client is None or not chosen:
        return RedirectResponse(back, status_code=_SEE_OTHER)
    if chosen.casefold() not in {k.casefold() for k in (client.keywords or [])}:
        update_client(session, client_id, keywords=[*(client.keywords or []), chosen])

    # Drop it from the offered list so the panel reflects what is left to decide.
    stored = themework.state.get(client_id)
    if stored and stored.get("state") == "fertig":
        stored["probes"] = [
            probe
            for probe in stored.get("probes", [])  # type: ignore[union-attr]
            if probe.term.casefold() != chosen.casefold()
        ]

    threading.Thread(
        target=_run_theme_radar,
        args=(client_id,),
        daemon=True,
        name=f"newspulse-radar-{client_id}",
    ).start()
    return RedirectResponse(back, status_code=_SEE_OTHER)


def _run_theme_radar(client_id: int) -> None:
    """Search a newly added theme straight away, so its hits are there to see."""
    try:
        with runlock.guard:
            with get_session() as session:
                client = session.get(Client, client_id)
                if client is None:
                    return
                linked = job.refresh_radar(session, client)
                _log.info("radar refresh for %r linked %d item(s)", client.name, linked)
    except Exception:  # noqa: BLE001 — a worker thread must never die silently
        _log.exception("radar refresh for client %s failed", client_id)


@router.post("/settings/clients/{client_id}/industry")
def suggest_industry_route(
    client_id: int, session: Session = Depends(get_db)
) -> RedirectResponse:
    """Propose a better industry term for one client, measured before offered."""
    themework.industry_job.start(session, client_id)
    return RedirectResponse(f"/settings?edit={client_id}", status_code=_SEE_OTHER)


@router.post("/settings/clients/{client_id}/industry/accept")
def accept_industry_route(
    client_id: int,
    term: str = Form(...),
    session: Session = Depends(get_db),
) -> RedirectResponse:
    """Adopt a measured industry term — added to what is there, never replacing it.

    Additive because the operator's own word is usually the accurate one and the
    measured one is merely the searchable one: "Beauty Tech" describes the mandate
    better than "Kosmetikindustrie" does, and the field takes both (it is split on
    semicolons and OR-joined). Overwriting what someone typed to make a search
    work is a trade they never agreed to.
    """
    client = session.get(Client, client_id)
    chosen = (term or "").strip()
    if client is not None and chosen:
        existing = [t for t in (client.industry or "").split(";") if t.strip()]
        if chosen.casefold() not in {t.strip().casefold() for t in existing}:
            update_client(
                session,
                client_id,
                industry="; ".join([*(t.strip() for t in existing), chosen]),
            )
    return RedirectResponse(f"/settings?edit={client_id}", status_code=_SEE_OTHER)


@router.post("/settings/clients/{client_id}/rivals")
def suggest_rivals_route(
    client_id: int, session: Session = Depends(get_db)
) -> RedirectResponse:
    """Propose competitors for one client, on a worker thread.

    Synchronous before, which meant a `claude -p` call inside the request: tens
    of seconds of a page doing nothing, and the operator's verdict was that it
    "funktioniert im Grunde gar nicht". The panel polls itself instead.

    The proposal opens beside the client's own edit row, because that is where
    everything else about a mandate is set — aliases, search terms, alert topics —
    and a competitor is configuration like the rest of them.
    """
    themework.rivals_job.start(session, client_id)
    return RedirectResponse(f"/settings?edit={client_id}", status_code=_SEE_OTHER)


@router.post("/client/{client_id}/competitors/accept")
def accept_rival_route(
    client_id: int,
    name: str = Form(...),
    redirect_to: str = Form(""),
    session: Session = Depends(get_db),
) -> RedirectResponse:
    """Turn one accepted proposal into a monitored competitor and link it.

    Creates the company as a competitor — monitored exactly like a mandate, which
    is what makes its mention count comparable — and links it to this client. An
    existing company of the same name is reused rather than duplicated, so a name
    proposed for two mandates ends up as one row watched by both.
    """
    # Same posture as the run trigger's redirect: only a same-site path is
    # honoured, so a crafted form cannot bounce the operator off the host.
    back = redirect_to if redirect_to.startswith("/") else f"/client/{client_id}"
    client = session.get(Client, client_id)
    proposed = (name or "").strip()
    if client is None or not proposed:
        return RedirectResponse(back, status_code=_SEE_OTHER)

    existing = session.scalars(
        select(Client).where(func.lower(Client.name) == proposed.lower())
    ).first()
    other = existing
    if other is None:
        # create_client owns the name/uniqueness rules; the role is a separate
        # field it does not take, so it is set immediately after — before the
        # company can appear anywhere as a mandate.
        other = create_client(session, name=proposed)
        update_client(session, other.id, is_competitor=True)
        _log.info("created %r as a competitor of %r", proposed, client.name)
    if other.id != client.id and other not in client.competitors:
        client.competitors.append(other)
        session.commit()
    return RedirectResponse(back, status_code=_SEE_OTHER)


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
    client_id: int,
    competitor_id: int,
    redirect_to: str = Form(""),
    session: Session = Depends(get_db),
) -> RedirectResponse:
    """Remove a company from this client's comparison set. The company itself and
    its archive are untouched — only the link is dropped.

    Returns to wherever the link was cut: the client page reads share of voice,
    the settings row configures it, and being thrown to the other one is a small
    but real way to lose your place. Same-site paths only.
    """
    back = (
        redirect_to
        if redirect_to.startswith("/") and "//" not in redirect_to
        else f"/client/{client_id}"
    )
    client = session.get(Client, client_id)
    if client is not None:
        other = session.get(Client, competitor_id)
        if other is not None and other in client.competitors:
            client.competitors.remove(other)
            session.commit()
    return RedirectResponse(back, status_code=_SEE_OTHER)


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
        _start_onboarding(created.id, created.name)
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
    muted_categories: list[str] = Form(default=[]),
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
        # Checkboxes: an unchecked box sends nothing, so the empty list is a real
        # answer ("mute nothing") and must be written rather than skipped.
        fields["muted_categories"] = [
            value for value in muted_categories if value in _CATEGORY_VALUES
        ]
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
