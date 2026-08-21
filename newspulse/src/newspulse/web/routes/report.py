"""Berichte: read the month, cut it down, release it, send it as a document.

Two surfaces and one rule between them.

The *review* surface is where a person does the only thing a model may not: read
each proposed finding against the coverage underneath it and decide whether the
agency is willing to say it. Every finding can be edited, kept or dropped, and a
dropped one stays on the page marked as dropped, with the reason it was dropped.
It does not disappear — what was proposed and rejected is part of how a report was
arrived at, and a claim that vanished on a click cannot be argued about at the
jour fixe.

The *document* is the artefact: a dated page with the period, the comparison set,
the figures and the surviving findings, each with the coverage it rests on. It is
served as a self-contained HTML page with no external request in it — the same
offline rule the vendored htmx follows — and the export is the same template with
a download header, so the file a client receives cannot say something the screen
did not.

The rule between them is the release. It means here exactly what it means in the
outreach ledger: a human read this and put the agency's name on it. After it, the
report is frozen — :func:`newspulse.report.release` copies the document into the
row — and nothing on this page can edit, drop, restore or regenerate it. A
document a client has been sent must still say next quarter what it said when it
was sent.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import prose, report as reports, reporting
from ...analyzer import AnalyzerError, ParseError, invoke_with_fallback
from ...models import Client, Report, ReportFinding
from ...reporting import Period
from ..app import get_db, templates
from .today import _fetch_last_run, _local_tz

router = APIRouter()

_SEE_OTHER = 303

#: How many months the period picker offers. A year, because this is a monthly
#: artefact: a consultant reaching further back is writing an annual review, which
#: is a different document with a different shape.
_PERIOD_CHOICES = 12

#: The generator a draft is written with. A module attribute rather than a default
#: argument so a test can substitute it in one place and no route has to thread it
#: through — the same seam :func:`newspulse.report.findings` already offers.
_generate: reports.Generate = invoke_with_fallback

#: What each finding kind is called on screen. The stored value is a lowercase
#: German word, and a template that title-cased it would produce a label no
#: translation could reach.
_KIND_LABELS: dict[str, str] = {
    "sichtbarkeit": "Sichtbarkeit",
    "risiko": "Risiko",
    "wirkung": "Wirkung",
    "botschaft": "Botschaft",
}

#: The two halves of the share-of-voice chart. The mandate is highlighted and
#: everything it is measured against is one neutral block: this is a report about
#: one company, and colouring five rivals differently would invite the reader to
#: compare them with each other on a page that is not about that.
_VOICE_TONE_MANDATE = "mandat"
_VOICE_TONE_FIELD = "umfeld"

#: Said on the review surface when the mandate has no report at all yet.
_NO_REPORT = (
    "Für diesen Mandanten liegt noch kein Bericht vor. Zum Monatsersten entsteht "
    "einer automatisch; er lässt sich hier auch sofort erzeugen."
)


# --- Periods --------------------------------------------------------------------


def _period_key(period: Period) -> str:
    """A period as the picker names it: ``2026-07``."""
    return f"{period.start.astimezone(_local_tz()):%Y-%m}"


def _period_from(value: str | None, now: dt.datetime) -> Period:
    """``2026-07`` as a period; anything unreadable falls back to last month.

    Unreadable rather than refused: the value comes from a query string, and a
    hand-edited URL should land the reader on the report they almost certainly
    wanted rather than on a 422.
    """
    match = re.fullmatch(r"(\d{4})-(\d{2})", (value or "").strip())
    if not match:
        return reports.previous_month(now)
    year, month = int(match[1]), int(match[2])
    if not 1 <= month <= 12:
        return reports.previous_month(now)
    return Period.month(year, month)


def _period_options(now: dt.datetime) -> list[str]:
    """The pickable months, newest first."""
    period = reports.previous_month(now)
    options = []
    for _ in range(_PERIOD_CHOICES):
        options.append(_period_key(period))
        earlier = period.start.astimezone(_local_tz()) - dt.timedelta(days=1)
        period = Period.month(earlier.year, earlier.month)
    return options


# --- Charts ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Segment:
    """One block of a bar, carrying its own number.

    ``text`` is on the page beside the block and again in the chart's table, so
    every value is readable without the colour. That is not a nicety here: a
    tonality split whose negative share can only be told from a hue is a figure a
    colour-blind consultant cannot check before it goes to a client.
    """

    label: str
    tone: str
    text: str
    #: Percent of the bar, 0..100.
    share: float


@dataclass(frozen=True, slots=True)
class Chart:
    """A bar and the table that says the same thing."""

    title: str
    #: The column head over the numbers in the table view.
    unit: str
    segments: tuple[Segment, ...]


def _tonality_chart(document: reports.Document) -> Chart | None:
    """The tonality split, one block per tone, each with its count."""
    total = document.tonality_total
    if not document.tonality or not total:
        return None
    return Chart(
        title="Tonalität",
        unit="Beiträge",
        segments=tuple(
            Segment(
                label=figure.subject,
                tone=figure.subject,
                text=figure.text,
                share=(figure.value or 0.0) / total * 100,
            )
            for figure in document.tonality
        ),
    )


def _voice_chart(document: reports.Document) -> Chart | None:
    """The mandate's part of its comparison set, against the rest of it.

    Two blocks and not one per rival. The denominator is named in full on the
    document; splitting it into five coloured slices would turn a report about one
    company into a league table nobody asked for.
    """
    share = next(
        (
            figure.value
            for figure in document.figures
            if figure.key == reporting.MetricKey.SHARE_OF_VOICE.value
        ),
        None,
    )
    if share is None:
        return None
    return Chart(
        title="Anteil am Marktgespräch",
        unit="Anteil",
        segments=(
            Segment(
                label=document.client_name,
                tone=_VOICE_TONE_MANDATE,
                text=f"{share * 100:.1f} %",
                share=share * 100,
            ),
            Segment(
                label="Vergleichsumfeld",
                tone=_VOICE_TONE_FIELD,
                text=f"{(1 - share) * 100:.1f} %",
                share=(1 - share) * 100,
            ),
        ),
    )


# --- Loading --------------------------------------------------------------------


def _client_or_404(session: Session, client_id: int) -> Client:
    client = session.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")
    return client


def _report_or_404(session: Session, client: Client, report_id: int) -> Report:
    """One report, and it has to be this mandate's.

    The ownership check is the point: a report id in a URL under another client
    would otherwise render one company's document inside another's workspace.
    """
    row = session.get(Report, report_id)
    if row is None or row.client_id != client.id:
        raise HTTPException(status_code=404, detail="Report not found")
    return row


def _editable(row: Report) -> Report:
    """The row, if it may still be changed. Otherwise 409 and the reason.

    409 rather than 403: nothing is wrong with the caller, the report is simply in
    a state that no longer accepts this. The same rule the outreach ledger applies
    to a released letter.
    """
    if reports.is_released(row):
        raise HTTPException(
            status_code=409,
            detail=(
                "Der Bericht ist freigegeben. Freigegebene Berichte werden nicht "
                "mehr geändert."
            ),
        )
    return row


def _reports_for(session: Session, client_id: int) -> list[Report]:
    """Every report this mandate has, newest period first."""
    return list(
        session.scalars(
            select(Report)
            .where(Report.client_id == client_id)
            .order_by(Report.period_start.desc())
        ).all()
    )


def _chosen_report(
    session: Session, client: Client, wanted: str | None, now: dt.datetime
) -> tuple[Report | None, str]:
    """The report the page is about, and the period key it sits under.

    With no ``wanted``, the newest report the mandate has — a consultant opening
    the tab wants the thing that is waiting for them, which after the monthly draft
    is last month's. Only when there is none at all does the picker fall back to
    the period a generation would produce.
    """
    existing = _reports_for(session, client.id)
    if wanted:
        period = _period_from(wanted, now)
        return reports.for_period(session, client.id, period), _period_key(period)
    if existing:
        return existing[0], _period_key(
            Period(existing[0].period_start, existing[0].period_end)
        )
    return None, _period_key(reports.previous_month(now))


# --- The review surface ---------------------------------------------------------


def _render(
    request: Request,
    session: Session,
    client: Client,
    row: Report | None,
    period_key: str,
    *,
    error: str = "",
    notice: str = "",
) -> HTMLResponse:
    document = reports.document(session, client, row) if row is not None else None
    return templates.TemplateResponse(
        request,
        "report_review.html",
        {
            "client": client,
            "report": row,
            "document": document,
            # Kept *and* dropped: this is the screen the dropping happens on, and
            # a rejected finding that vanished cannot be argued about afterwards.
            "views": reports.resolve(session, row, kept_only=False) if row else [],
            "released": reports.is_released(row) if row is not None else False,
            "kind_labels": _KIND_LABELS,
            "periods": _period_options(dt.datetime.now(dt.UTC)),
            "period_key": period_key,
            "reports": _reports_for(session, client.id),
            "no_report": _NO_REPORT,
            "error": error,
            "notice": notice,
            "last_run": _fetch_last_run(session),
            "header_date": dt.datetime.now(_local_tz()).date(),
        },
    )


@router.get("/client/{client_id}/berichte", response_class=HTMLResponse)
def review(
    request: Request,
    client_id: int,
    zeitraum: str | None = None,
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """The findings for one period, with the evidence under each of them."""
    client = _client_or_404(session, client_id)
    row, period_key = _chosen_report(
        session, client, zeitraum, dt.datetime.now(dt.UTC)
    )
    return _render(request, session, client, row, period_key)


@router.post("/client/{client_id}/berichte/erzeugen")
def generate(
    request: Request,
    client_id: int,
    zeitraum: str = Form(""),
    session: Session = Depends(get_db),
) -> Response:
    """Read the period and propose findings, replacing any existing draft.

    A failed generation renders the page with the reason on it rather than
    redirecting: "nothing to say about this month" and "the call failed" must not
    look alike to somebody who is about to send a document.
    """
    client = _client_or_404(session, client_id)
    period = _period_from(zeitraum, dt.datetime.now(dt.UTC))
    key = _period_key(period)
    try:
        draft = reports.findings(session, client, period, generate=_generate)
        reports.store(session, client, draft)
    except reports.ReportReleased as exc:
        existing = reports.for_period(session, client.id, period)
        return _render(request, session, client, existing, key, error=str(exc))
    except (AnalyzerError, ParseError) as exc:
        existing = reports.for_period(session, client.id, period)
        return _render(
            request,
            session,
            client,
            existing,
            key,
            error=f"Der Bericht ist fehlgeschlagen: {exc}",
        )
    return RedirectResponse(
        f"/client/{client_id}/berichte?zeitraum={key}", status_code=_SEE_OTHER
    )


def _finding_or_404(row: Report, finding_id: int) -> ReportFinding:
    for finding in row.findings:
        if finding.id == finding_id:
            return finding
    raise HTTPException(status_code=404, detail="Finding not found")


@router.post("/client/{client_id}/berichte/{report_id}/befund/{finding_id}")
def edit_finding(
    request: Request,
    client_id: int,
    report_id: int,
    finding_id: int,
    claim: str = Form(""),
    consequence: str = Form(""),
    session: Session = Depends(get_db),
) -> Response:
    """Rewrite one finding's sentence.

    Two rules survive the edit. The house style is applied rather than requested,
    exactly as it is on a generated claim — this text goes to a client under the
    agency's name. And the sentence still may not state a figure RauteOS cannot
    source: DEC-2 settled that reach is absent rather than estimated, and a rule
    that only bound the model would be a rule the report does not have.
    """
    client = _client_or_404(session, client_id)
    row = _editable(_report_or_404(session, client, report_id))
    finding = _finding_or_404(row, finding_id)
    period_key = _period_key(Period(row.period_start, row.period_end))

    written = prose.plain(claim).strip()
    follows = prose.plain(consequence).strip()
    if not written:
        return _render(
            request, session, client, row, period_key,
            error="Ein Befund ohne Aussage ist kein Befund. Der Text bleibt wie er war.",
        )
    stated = reporting.forbidden_terms(f"{written}\n{follows}")
    if stated:
        return _render(
            request, session, client, row, period_key,
            error=(
                f"„{', '.join(stated)}“ kann RauteOS aus Archiv und Ledger nicht "
                "belegen und steht deshalb in keinem Bericht. Der Text bleibt wie "
                "er war."
            ),
        )
    finding.claim = written
    finding.consequence = follows
    finding.edited_at = dt.datetime.now(dt.UTC)
    session.commit()
    return RedirectResponse(
        f"/client/{client_id}/berichte?zeitraum={period_key}", status_code=_SEE_OTHER
    )


@router.post("/client/{client_id}/berichte/{report_id}/befund/{finding_id}/verwerfen")
def drop_finding(
    client_id: int,
    report_id: int,
    finding_id: int,
    grund: str = Form(""),
    session: Session = Depends(get_db),
) -> Response:
    """Drop a finding out of the document. The row stays, marked, with its reason."""
    client = _client_or_404(session, client_id)
    row = _editable(_report_or_404(session, client, report_id))
    finding = _finding_or_404(row, finding_id)
    finding.kept = False
    finding.drop_reason = prose.plain(grund).strip()
    session.commit()
    return RedirectResponse(
        f"/client/{client_id}/berichte"
        f"?zeitraum={_period_key(Period(row.period_start, row.period_end))}",
        status_code=_SEE_OTHER,
    )


@router.post("/client/{client_id}/berichte/{report_id}/befund/{finding_id}/behalten")
def keep_finding(
    client_id: int,
    report_id: int,
    finding_id: int,
    session: Session = Depends(get_db),
) -> Response:
    """Take a dropped finding back into the document, and clear why it was out."""
    client = _client_or_404(session, client_id)
    row = _editable(_report_or_404(session, client, report_id))
    finding = _finding_or_404(row, finding_id)
    finding.kept = True
    finding.drop_reason = ""
    session.commit()
    return RedirectResponse(
        f"/client/{client_id}/berichte"
        f"?zeitraum={_period_key(Period(row.period_start, row.period_end))}",
        status_code=_SEE_OTHER,
    )


@router.post("/client/{client_id}/berichte/{report_id}/freigeben")
def release(
    client_id: int,
    report_id: int,
    freigegeben_von: str = Form(""),
    session: Session = Depends(get_db),
) -> Response:
    """Put the agency's name on it, and freeze it.

    The freeze is not done here: :func:`newspulse.report.release` builds the
    document and stores it, so there is no route that can release without freezing
    and no second copy of that rule to fall out of step.
    """
    client = _client_or_404(session, client_id)
    row = _report_or_404(session, client, report_id)
    reports.release(session, row, by=freigegeben_von)
    return RedirectResponse(
        f"/client/{client_id}/berichte/{report_id}/dokument", status_code=_SEE_OTHER
    )


# --- The document ---------------------------------------------------------------


def _document_context(
    client: Client, row: Report, document: reports.Document, *, download: bool
) -> dict:
    """Everything the artefact renders from, identical for screen and export."""
    return {
        "client": client,
        "report_id": row.id,
        "document": document,
        "tonality_chart": _tonality_chart(document),
        "voice_chart": _voice_chart(document),
        "kind_labels": _KIND_LABELS,
        # The only thing the two differ by, and it sits outside the document body:
        # the on-screen page carries links back into the app, the downloaded file
        # carries none, and neither changes a word of what the report says.
        "download": download,
    }


def _filename(client: Client, document: reports.Document) -> str:
    """A downloaded report's filename: mandate and period, both readable."""
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", client.name).strip("_") or "mandant"
    stamp = f"{document.period_start.astimezone(_local_tz()):%Y-%m}"
    return f"bericht_{safe}_{stamp}.html"


@router.get(
    "/client/{client_id}/berichte/{report_id}/dokument", response_class=HTMLResponse
)
def document(
    request: Request,
    client_id: int,
    report_id: int,
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """The report as it is sent: dated, with the evidence under every claim."""
    client = _client_or_404(session, client_id)
    row = _report_or_404(session, client, report_id)
    return templates.TemplateResponse(
        request,
        "report_document.html",
        _document_context(
            client, row, reports.document(session, client, row), download=False
        ),
    )


@router.get("/client/{client_id}/berichte/{report_id}/dokument.html")
def export(
    request: Request,
    client_id: int,
    report_id: int,
    session: Session = Depends(get_db),
) -> Response:
    """The same document as a file. The same template, so it cannot say more."""
    client = _client_or_404(session, client_id)
    row = _report_or_404(session, client, report_id)
    rendered = reports.document(session, client, row)
    return templates.TemplateResponse(
        request,
        "report_document.html",
        _document_context(client, row, rendered, download=True),
        headers={
            "Content-Disposition": (
                f'attachment; filename="{_filename(client, rendered)}"'
            )
        },
    )
