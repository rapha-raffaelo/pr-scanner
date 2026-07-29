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
from sqlalchemy.orm import Session

from ... import advisor
from ...db import get_session
from ...models import Client
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
    """Generate and store a brief on a worker thread; always release the lock."""
    try:
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


@router.get("/client/{client_id}/advice", response_class=HTMLResponse)
def advice_view(
    request: Request, client_id: int, session: Session = Depends(get_db)
) -> HTMLResponse:
    """The client's current brief, or an invitation to generate the first one."""
    client = session.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")

    latest = advisor.latest(session, client_id)
    coverage = advisor.recent_coverage(session, client_id, days=advisor.DEFAULT_DAYS)
    return templates.TemplateResponse(
        request,
        "advice.html",
        {
            "client": client,
            "advisory": latest,
            # Resolve each suggestion's evidence ids back to the coverage they
            # cite, so a recommendation can be checked rather than trusted.
            "coverage": {ref.index: ref for ref in coverage},
            "available": len(coverage),
            "running": _generating.locked(),
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
    return RedirectResponse(f"/client/{client_id}/advice", status_code=_SEE_OTHER)
