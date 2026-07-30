"""The communications guide page: write it, or upload what already exists.

Four actions and one rule between them — nothing a document says is applied
without a person seeing it first. The distillation produces a *proposal* that is
rendered beside the current guide; saving is a separate, deliberate click. That is
the same posture the client import takes, and for the same reason: an uploaded
document can contradict what is already there, and only a person can settle that.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from ... import guide
from ...analyzer import AnalyzerError
from ...models import Client
from ..app import get_db, templates
from .today import _fetch_last_run, _local_tz

router = APIRouter()

_SEE_OTHER = 303


def _render(
    request: Request,
    session: Session,
    client: Client,
    *,
    proposal: str | None = None,
    error: str | None = None,
    saved: bool = False,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "client_guide.html",
        {
            "client": client,
            "guide_text": client.comms_guide or "",
            "sources": guide.sources(session, client.id),
            "max_chars": guide.GUIDE_MAX_CHARS,
            "max_mb": guide.MAX_UPLOAD_BYTES // (1024 * 1024),
            "proposal": proposal,
            "guide_error": error,
            "saved": saved,
            "last_run": _fetch_last_run(session),
            "header_date": dt.datetime.now(_local_tz()).date(),
        },
    )


def _client_or_404(session: Session, client_id: int) -> Client:
    client = session.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")
    return client


@router.get("/client/{client_id}/guide", response_class=HTMLResponse)
def guide_view(
    request: Request, client_id: int, session: Session = Depends(get_db)
) -> HTMLResponse:
    """The guide as it stands, its sources, and the way to change either."""
    return _render(request, session, _client_or_404(session, client_id))


@router.post("/client/{client_id}/guide")
def save_guide(
    request: Request,
    client_id: int,
    comms_guide: str = Form(""),
    session: Session = Depends(get_db),
) -> Response:
    """Store the edited guide. Over-long text is trimmed, never rejected —
    a consultant pasting a long passage should get a saved guide and a visible
    counter, not a lost edit."""
    client = _client_or_404(session, client_id)
    guide.save(session, client, comms_guide)
    return RedirectResponse(f"/client/{client_id}/guide?saved=1", status_code=_SEE_OTHER)


@router.post("/client/{client_id}/guide/upload")
async def upload_source(
    request: Request, client_id: int, session: Session = Depends(get_db)
) -> Response:
    """Read a document into text and keep it as a source.

    Nothing is distilled here. Upload and interpretation are separate steps so a
    failed extraction is reported against the file rather than surfacing later as
    a mysteriously thin proposal.
    """
    client = _client_or_404(session, client_id)
    form = await request.form()
    upload = form.get("file")
    filename = getattr(upload, "filename", "") or ""
    if not filename:
        return _render(request, session, client, error="Keine Datei gewählt.")
    try:
        text = guide.extract_text(filename, await upload.read())
    except guide.ExtractionError as exc:
        return _render(request, session, client, error=str(exc))
    guide.store_source(session, client, filename, text)
    return RedirectResponse(f"/client/{client_id}/guide", status_code=_SEE_OTHER)


@router.post("/client/{client_id}/guide/distill")
def distill_guide(
    request: Request, client_id: int, session: Session = Depends(get_db)
) -> Response:
    """Propose a guide from the uploaded documents — shown, not stored."""
    client = _client_or_404(session, client_id)
    try:
        proposal = guide.distill(session, client)
    except guide.ExtractionError as exc:
        return _render(request, session, client, error=str(exc))
    except AnalyzerError as exc:
        return _render(
            request, session, client, error=f"Der Vorschlag ist fehlgeschlagen: {exc}"
        )
    return _render(request, session, client, proposal=proposal)


@router.post("/client/{client_id}/guide/sources/{source_id}/remove")
def remove_source(
    client_id: int, source_id: int, session: Session = Depends(get_db)
) -> Response:
    """Drop a source document. The guide itself stays — by then it is edited text."""
    _client_or_404(session, client_id)
    guide.delete_source(session, client_id, source_id)
    return RedirectResponse(f"/client/{client_id}/guide", status_code=_SEE_OTHER)
