"""The Today view: today's coverage across all clients (DEC-1 option C).

Two-pane triage layout — a narrow left rail of alert cards (the must-see) beside
a full ranked feed on the right. The page defaults to the current local day with
a date picker for any prior day; a day with no coverage renders a clean empty
state rather than an error.
"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import Analysis, Article, Client, Run
from ..app import get_db, templates

router = APIRouter()

# The date-picker exchange format; also how a prior day is passed as ?date=.
_DATE_FORMAT = "%Y-%m-%d"

# Only surface analyses the analyzer judged relevant to the client. A relevance
# of 0 means "this story does not concern this client" — a row the future
# analyzer may still persist for a non-matching (article, client) pair — so it
# must never appear as coverage. Importance ordering is applied on top of this
# gate. (The analyzer contract is not yet built; this pins the include rule.)
_MIN_RELEVANCE = 1

# The zoneinfo db stores each zone under a ".../zoneinfo/<Area>/<City>" path;
# the tail after this marker is the IANA name of an /etc/localtime symlink.
_ZONEINFO_MARKER = "zoneinfo/"


def _resolve_local_zone() -> dt.tzinfo:
    """Resolve the machine's local zone as a DST-aware ``ZoneInfo``.

    A DST-aware zone is required, not the fixed offset that
    ``datetime.now().astimezone().tzinfo`` returns: that offset reflects the DST
    state *right now* and, applied to a day in the other DST regime, shifts the
    local-day window by the DST delta (±1h), misattributing articles near local
    midnight. Prefers the ``TZ`` env var, then the ``/etc/localtime`` symlink
    target; falls back to the current fixed offset (DST-naive) only if neither
    yields a known zone, so the dashboard never crashes on an exotic host.
    """
    tz_name = os.environ.get("TZ")
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    localtime = Path("/etc/localtime")
    if localtime.is_symlink():
        target = str(localtime.readlink())
        if _ZONEINFO_MARKER in target:
            try:
                return ZoneInfo(target.split(_ZONEINFO_MARKER, 1)[1])
            except (ZoneInfoNotFoundError, ValueError):
                pass
    # Last resort: the current fixed offset. astimezone() always yields a
    # concrete tzinfo; fall back to UTC defensively.
    return dt.datetime.now().astimezone().tzinfo or dt.UTC


# Resolved once at import: the host's zone does not change during a process, and
# a ZoneInfo (not a frozen offset) is what makes _day_bounds_utc DST-correct for
# any requested day.
_LOCAL_ZONE = _resolve_local_zone()


@dataclass(frozen=True, slots=True)
class TodayItem:
    """One rendered coverage row (one article as it concerns one client)."""

    headline: str
    url: str
    source: str
    published_at: dt.datetime
    summary: str | None
    category: str
    importance: int
    client_name: str
    is_alert: bool


@dataclass(frozen=True, slots=True)
class RunStatusView:
    """Header last-run status sourced from the latest ``runs`` row."""

    ran_at: dt.datetime
    is_running: bool
    status: str
    articles_checked: int
    feed_errors: int


def _local_tz() -> dt.tzinfo:
    """The machine's local timezone — 'today' is the current *local* day.

    A single DST-aware zone (resolved once at import), so bounding any requested
    day uses that day's own offset rather than today's frozen one.
    """
    return _LOCAL_ZONE


def _parse_day(raw: str | None) -> dt.date:
    """Resolve the requested day: a valid ``?date=YYYY-MM-DD`` or today (local).

    An unparseable value falls back to today rather than erroring, so a hand-typed
    or stale URL never 500s the dashboard.
    """
    if raw:
        try:
            return dt.datetime.strptime(raw, _DATE_FORMAT).date()
        except ValueError:
            pass
    return dt.datetime.now(_local_tz()).date()


def _day_bounds_utc(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """UTC [start, end) covering the given *local* calendar day."""
    tz = _local_tz()
    start_local = dt.datetime.combine(day, dt.time.min, tzinfo=tz)
    end_local = start_local + dt.timedelta(days=1)
    return start_local.astimezone(dt.UTC), end_local.astimezone(dt.UTC)


def _fetch_items(session: Session, day: dt.date) -> list[TodayItem]:
    """Coverage for ``day`` ordered for triage: alerts first, then importance desc.

    Ordering is ``is_alert`` desc so alerts surface above non-alerts (also shown
    in the left rail), then ``importance_score`` desc, with ``published_at`` desc
    as a stable tiebreak.
    """
    tz = _local_tz()
    start_utc, end_utc = _day_bounds_utc(day)
    stmt = (
        select(Analysis, Article, Client)
        .join(Article, Analysis.article_id == Article.id)
        .join(Client, Analysis.client_id == Client.id)
        .where(
            Article.published_at >= start_utc,
            Article.published_at < end_utc,
            Analysis.relevance_score >= _MIN_RELEVANCE,
        )
        .order_by(
            Analysis.is_alert.desc(),
            Analysis.importance_score.desc(),
            Article.published_at.desc(),
        )
    )
    return [
        TodayItem(
            headline=article.title,
            url=article.url,
            source=article.source,
            # Stored UTC, shown in local time — the page frames "today" locally.
            published_at=article.published_at.astimezone(tz),
            summary=analysis.summary,
            category=analysis.category.value,
            importance=analysis.importance_score,
            client_name=client.name,
            is_alert=analysis.is_alert,
        )
        for analysis, article, client in session.execute(stmt).all()
    ]


def _fetch_last_run(session: Session) -> RunStatusView | None:
    """The most recent run for the header, or None if the job never ran."""
    run: Run | None = session.execute(
        select(Run).order_by(Run.started_at.desc()).limit(1)
    ).scalar_one_or_none()
    if run is None:
        return None
    # Shown in local time so the header matches the article times and day window
    # (all local); a run with no finished_at is still in progress.
    ran_at = (run.finished_at or run.started_at).astimezone(_local_tz())
    return RunStatusView(
        ran_at=ran_at,
        is_running=run.finished_at is None,
        status=run.status.value,
        articles_checked=run.articles_found,
        feed_errors=len(run.errors),
    )


@router.get("/", response_class=HTMLResponse)
def today_view(
    request: Request,
    date: str | None = None,
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """Render the Today view for the current local day (or ``?date=``)."""
    day = _parse_day(date)
    items = _fetch_items(session, day)
    alerts = [item for item in items if item.is_alert]
    return templates.TemplateResponse(
        request,
        "today.html",
        {
            "day": day,
            "day_iso": day.strftime(_DATE_FORMAT),
            "items": items,
            "alerts": alerts,
            "last_run": _fetch_last_run(session),
        },
    )
