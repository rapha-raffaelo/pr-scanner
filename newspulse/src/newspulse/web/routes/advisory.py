"""The advisor view: generated PR action suggestions for one client.

Generation is explicit — a button, not a side effect of the daily run. Three
reasons: it costs a model call per client, the advice is only worth reading when
someone is about to act on it, and a brief that regenerates itself silently would
change under the operator between opening the page and reading it.

The result is persisted (``advisories``) so the page is instant on reload and the
brief that was current during a crisis stays on the record.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ... import advisor, angles, job
from ...db import get_session
from ..runlock import guard as _run_guard
from ...models import Client, TopicHit
from ..app import get_db, templates
from .today import _fetch_last_run, _local_tz

router = APIRouter()

_log = logging.getLogger(__name__)
_SEE_OTHER = 303

# One generation at a time, process-wide. Each is a `claude -p` call taking tens
# of seconds; a second click while one is running would spend a second call to
# produce a brief that overwrites the first.
_generating = threading.Lock()


def _run_advisory(client_id: int, days: int) -> None:
    """Generate and store a brief on a worker thread; always release the lock.

    Holds the sweep's guard as well, which is what makes the page refresh itself:
    the header polls while that guard is held and reloads the whole page when it is
    released. Without it the reader was told to reload by hand — an instruction the
    machinery had already made unnecessary everywhere else.
    """
    try:
        _run_guard.acquire()
        with get_session() as session:
            client = session.get(Client, client_id)
            if client is None:
                return
            brief, coverage = advisor.advise(session, client, days=days)
            advisor.store(session, client, brief, coverage, days=days)
            _log.info(
                "advisory generated for %r: %d suggestion(s) from %d article(s)",
                client.name,
                len(brief.suggestions),
                len(coverage),
            )
    except Exception:  # noqa: BLE001 — a worker thread must never die silently
        _log.exception("advisory generation failed")
    finally:
        _generating.release()
        # Released after the brief is committed, so the reload the header triggers
        # finds the finished result rather than an empty page a beat too early.
        try:
            _run_guard.release()
        except RuntimeError:  # never acquired, e.g. an error before the try body
            pass


#: The windows the page offers. The count beside the button, the window a
#: generation uses and the option the select shows all read from the same value —
#: they disagreed before, which is how a page could report "0 Artikel" for thirty
#: days directly under a brief that had just been generated over ninety.
# The windows the reader can choose. Half a year is included because a mandate
# with sparse coverage still has a pattern — it just takes longer to see, and the
# alternative on offer was an empty page.
ADVICE_DAYS = (7, 30, 90, 180)


def _parse_days(raw: str | None) -> int:
    """One of the offered windows, or the default. An unknown value falls back
    rather than erroring: a hand-typed or stale URL must not 500 the page."""
    try:
        value = int(raw or "")
    except (TypeError, ValueError):
        return advisor.DEFAULT_DAYS
    return value if value in ADVICE_DAYS else advisor.DEFAULT_DAYS



@router.get("/client/{client_id}/advice", response_class=HTMLResponse)
def advice_view(
    request: Request,
    client_id: int,
    days: str | None = None,
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """The client's current brief, or an invitation to generate the first one."""
    client = session.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")

    window = _parse_days(days)
    latest = advisor.latest(session, client_id)
    coverage = advisor.recent_coverage(session, client_id, days=window)
    return templates.TemplateResponse(
        request,
        "advice.html",
        {
            "client": client,
            "advisory": latest,
            # Resolve each suggestion's evidence ids back to the coverage they
            # cite, so a recommendation can be checked rather than trusted.
            "coverage": {ref.index: ref for ref in coverage},
            # What the window holds, not what the prompt was handed: the two
            # differ once the cap bites, and the label sits next to the control
            # for choosing the window. Reporting the cap made 30, 90 and 180 days
            # all read "40 Artikel", so widening looked like it did nothing.
            "available": advisor.coverage_count(session, client_id, days=window),
            "shown": len(coverage),
            # Echoed back so the select keeps the reader's choice: it always
            # rendered "Letzte 30 Tage" no matter what had just been asked for.
            "days": window,
            "day_options": ADVICE_DAYS,
            "running": _generating.locked(),
            "drafting": _drafting.locked(),
            # Why the last click came back empty, if it did. Shown instead of the
            # generic "the radar has collected nothing", which was wrong as often
            # as it was right.
            "impulse_refusal": _last_refusal.get(client_id),
            # What to offer when there is no coverage to advise on. An advisory
            # reads a client's own press; a young mandate has none, and telling it
            # "nothing to recommend" is true but useless. The impulse works off the
            # market instead, which is exactly the case this page cannot serve.
            "angles": angles.for_client(session, client_id),
            "latest_angle": angles.latest(session, client_id),
            # Whether a radar is possible at all, which is a question about the
            # client's themes — not about whether it has found anything yet. Read
            # off the hit count, a mandate with twenty-five themes and a radar that
            # has simply not run yet was told it had no radar.
            "has_themes": bool(client.keywords or client.alert_topics),
            # Rendered rather than written into the template, so the sentence and
            # the window can never disagree.
            "impulse_days": job.IMPULSE_LOOKBACK.days,
            "market_seen": session.scalar(
                select(func.count()).select_from(TopicHit).where(
                    TopicHit.client_id == client_id
                )
            ) or 0,
            "last_run": _fetch_last_run(session),
            "header_date": dt.datetime.now(_local_tz()).date(),
        },
    )


@router.post("/client/{client_id}/advice")
def generate_advice(
    client_id: int,
    request: Request,
    days: int = Form(advisor.DEFAULT_DAYS),
    session: Session = Depends(get_db),
) -> Response:
    """Kick off generation on a worker thread and return to the brief."""
    if session.get(Client, client_id) is None:
        raise HTTPException(status_code=404, detail="Client not found")
    if _generating.acquire(blocking=False):
        threading.Thread(
            target=_run_advisory,
            args=(client_id, max(1, days)),
            daemon=True,
            name="newspulse-advisory",
        ).start()
    return RedirectResponse(
        f"/client/{client_id}/advice?days={max(1, days)}", status_code=_SEE_OTHER
    )


# One impulse at a time, process-wide: the draft shells out to `claude` and a
# second click would spend a second call on the same question.
_drafting = threading.Lock()

# Why the last click produced nothing, per client. The draft runs on a worker
# thread and the page is a later request, so the reason needs somewhere to wait —
# and "nothing happened" with no explanation is the single thing that made this
# button look broken. Deliberately in memory and deliberately not a schema
# change: it describes one click, not the mandate, and going stale on restart is
# the correct behaviour for a "what just happened" message.
_last_refusal: dict[int, str] = {}


def _run_impulse(client_id: int) -> None:
    """Draft one impulse on a worker thread; always release the guard.

    Holds the sweep's guard as well, so the header's wheel covers the wait and a
    sweep cannot start mid-draft and race it on the same articles.
    """
    try:
        with _run_guard:
            with get_session() as session:
                client = session.get(Client, client_id)
                if client is None:
                    return
                _last_refusal.pop(client_id, None)
                drafted = job.draft_impulse(
                    session,
                    client,
                    note=lambda reason: _last_refusal.__setitem__(client_id, reason),
                )
                if drafted:
                    _last_refusal.pop(client_id, None)
                _log.info(
                    "impulse request for %r: %s",
                    client.name,
                    "drafted" if drafted else "no opening found",
                )
    except Exception as exc:  # noqa: BLE001 — a worker thread must never die silently
        # A crash must not read as "nothing worth saying": the reader would go on
        # believing the market was quiet.
        _last_refusal[client_id] = (
            f"Der Entwurf ist mit einem Fehler abgebrochen: {exc}. "
            "Details stehen im Log."
        )
        _log.exception("impulse request failed")
    finally:
        _drafting.release()


@router.post("/client/{client_id}/impulse")
def request_impulse(client_id: int, session: Session = Depends(get_db)) -> Response:
    """Draft an impulse now, from this client's themes.

    The sweep only drafts from material that arrived that morning, which leaves a
    mandate with nothing to show on a quiet day even though its field may have
    plenty worth saying. This asks the question directly, over a wider window.
    """
    if session.get(Client, client_id) is None:
        raise HTTPException(status_code=404, detail="Client not found")
    if _drafting.acquire(blocking=False):
        threading.Thread(
            target=_run_impulse,
            args=(client_id,),
            daemon=True,
            name=f"newspulse-impulse-{client_id}",
        ).start()
    return RedirectResponse(f"/client/{client_id}/advice", status_code=_SEE_OTHER)
