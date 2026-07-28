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
from ...outlets import tier_for
from ...stories import cluster
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

    # The analysis row this came from: triage state is per (article, client), so
    # a workflow action targets the analysis, not the article.
    analysis_id: int
    triage_state: str
    author: str | None
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
    in the left rail), then importance desc, with ``published_at`` desc as a
    stable tiebreak.

    Outlet tier (:mod:`newspulse.outlets`) breaks ties *within* an importance
    score, so among several 7/10 stories the FAZ piece sits above an automated
    share-price item. It only ever reorders: the displayed score stays the
    model's own, and no story is hidden or demoted out of view — weighting the
    score itself was measured on real coverage and lost genuine stories.

    Ranking is finished in Python rather than SQL because tier is a lookup over
    an editable table, not a column; a day's coverage is small enough that
    sorting it in memory costs less than denormalising it into the schema.
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
            # Mandates only. A competitor is monitored so its volume can be
            # compared, not so it lands in the morning triage queue — coverage
            # of a rival is not work, and mixing the two makes the day look
            # busier than it is.
            Client.is_competitor.is_(False),
        )
        .order_by(
            Analysis.is_alert.desc(),
            Analysis.importance_score.desc(),
            Article.published_at.desc(),
        )
    )
    items = [
        TodayItem(
            analysis_id=analysis.id,
            triage_state=analysis.triage_state.value,
            author=article.author,
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
    # The SQL ORDER BY already settled alerts-first, importance, and the
    # published_at tiebreak. Re-sort to slot tier in *below* importance — a lower
    # tier number is the better outlet, so it is negated to sort descending with
    # the rest. Python's sort is stable, so the published_at tiebreak survives.
    items.sort(
        key=lambda item: (item.is_alert, item.importance, -tier_for(item.source)),
        reverse=True,
    )
    return items


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
    category: str | None = None,
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """Render the Today view for the current local day (or ``?date=``).

    ``?category=`` narrows the day to one category. Filtering happens after the
    day is fetched, so the category dropdown can offer exactly the categories
    *this day* actually contains rather than the whole enum — an option that
    would return nothing is worse than no option.
    """
    day = _parse_day(date)
    all_items = _fetch_items(session, day)
    # Ordered by the day's own ranking, so the dropdown lists the busiest
    # categories in the order the reader already sees them.
    present = list(dict.fromkeys(item.category for item in all_items))

    selected = (category or "").strip()
    if selected not in present:
        # An unknown or stale category degrades to "no filter" rather than an
        # empty page a reader cannot explain.
        selected = ""
    items = [i for i in all_items if i.category == selected] if selected else all_items

    # Group the day's coverage into stories so one syndicated event occupies one
    # slot carrying its pickup count, rather than crowding the rail with copies.
    # Clustered after filtering, so a story's pickup count reflects what the
    # reader is actually looking at.
    stories = cluster(items)
    # A story is an alert if *any* copy of it fired: the alert may have been
    # computed on a copy that is not the lead.
    alerts = [s for s in stories if any(m.is_alert for m in s.members)]

    return templates.TemplateResponse(
        request,
        "today.html",
        {
            "day": day,
            "day_iso": day.strftime(_DATE_FORMAT),
            "items": items,
            "stories": stories,
            "alerts": alerts,
            "categories": present,
            "selected_category": selected,
            "hidden_count": len(all_items) - len(items),
            "last_run": _fetch_last_run(session),
        },
    )
