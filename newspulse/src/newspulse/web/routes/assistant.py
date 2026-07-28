"""Captain Comms: a streaming PR-strategy chat over the coverage on screen.

The advisor page answers one fixed question ("what should we do about this
client") and takes a minute to do it. This is the other half: any question, from
any page, with the coverage already on screen supplied as context — so "why is
H&M outperforming us?" does not require re-explaining who H&M is.

Two properties make it usable rather than a novelty:

* **It streams.** A drawer that paints nothing for 60 seconds reads as broken,
  and unlike a page you cannot navigate away from it.
* **It knows the page.** The client, the day and the visible coverage are built
  into the prompt server-side, not typed by the reader.

It remains advisory. Nothing here writes to the database, sends anything, or
changes what the tool does next — it produces text a person reads and acts on.
"""

from __future__ import annotations

import datetime as dt
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import Analysis, Article, Client
from ...streaming import StreamEvent, stream_claude
from ..app import get_db
from .today import _day_bounds_utc, _local_tz, _parse_day

router = APIRouter()

_MIN_RELEVANCE = 1

# Enough coverage for a grounded answer, few enough to keep one call inside the
# stream timeout. Ranked by importance, so the cap drops trivia and not the news.
_MAX_CONTEXT_ITEMS = 30

# A question is a question, not a payload. Anything longer is a paste accident,
# and the model's useful context budget is spent on coverage, not prose.
_MAX_QUESTION_CHARS = 500

# The character's own thesis, and the reason it fits this tool: Captain Comms
# argues that there are two kinds of communication — the kind that asserts
# something, and the kind that can prove it. A monitoring archive is exactly the
# evidence half of that, so the persona is not decoration: it is the standard the
# answers are held to. Vague counsel ("Stakeholder einbinden") is precisely what
# the character exists to reject.
_SYSTEM_FRAME = """Du bist Captain Comms, Kommunikationsstratege, und berätst die
PR-Beraterin, die diesen Mandanten betreut.

Dein Grundsatz: Es gibt zwei Arten von Kommunikation — die, die etwas behauptet,
und die, die etwas belegt. Zahlen sagen mehr als jede Selbstdarstellung. Du
argumentierst ausschließlich aus der Berichterstattung heraus, nie aus dem
Bauch.

Halte dich an diese Regeln:

- Belege jede Aussage. Nenne Zahlen aus dem Kontext — wie viele Meldungen, welche
  Medien, welcher Zeitraum. Eine Einschätzung ohne Beleg ist eine Behauptung, und
  Behauptungen sind genau das, was du nicht lieferst.
- Denke strategisch, nicht referierend. Sie kann die Meldungen selbst lesen; dein
  Beitrag ist die Einordnung: welches Narrativ entsteht, wer es treibt, wohin es
  läuft, was das für die Positionierung des Mandanten bedeutet.
- Sei konkret. "Stakeholder einbinden" ist keine Empfehlung. Sag WAS, GEGENÜBER
  WEM und MIT WELCHER BOTSCHAFT.
- Empfiehl auch Schweigen, wenn Schweigen richtig ist, und begründe es.
- Gibt die Berichterstattung die Frage nicht her, sag das offen. "Dazu liegt
  nichts vor" ist eine bessere Antwort als eine geratene.
- Du siehst nur Schlagzeilen und Kurzfassungen, nicht die vollständigen Artikel.
  Formuliere entsprechend vorsichtig über Details.
- Antworte knapp und auf Deutsch, in ganzen Sätzen, ohne Aufzählungswüsten."""

# Turns of the running conversation replayed back to the model. The CLI is
# stateless per call, so continuity has to be supplied — capped, because an
# unbounded transcript would crowd out the coverage that grounds the answer.
_MAX_HISTORY_TURNS = 6
_MAX_HISTORY_CHARS = 4_000


def _coverage_lines(
    session: Session, *, client_id: int | None, day: dt.date | None
) -> tuple[str, str]:
    """``(label, rendered coverage)`` for the page the reader is on.

    A day is used when one is being viewed, otherwise the client's recent
    coverage — the context should match what is on screen, or the answer will
    describe a different set of articles than the reader can see.
    """
    conditions = [Analysis.relevance_score >= _MIN_RELEVANCE]
    label_parts: list[str] = []

    if client_id is not None:
        client = session.get(Client, client_id)
        if client is not None:
            conditions.append(Analysis.client_id == client_id)
            label_parts.append(client.name)
    else:
        conditions.append(
            Analysis.client_id.in_(
                select(Client.id).where(Client.is_competitor.is_(False))
            )
        )

    if day is not None:
        start, end = _day_bounds_utc(day)
        conditions += [Article.published_at >= start, Article.published_at < end]
        label_parts.append(day.strftime("%d.%m.%Y"))
    else:
        conditions.append(
            Article.published_at >= dt.datetime.now(dt.UTC) - dt.timedelta(days=30)
        )
        label_parts.append("letzte 30 Tage")

    rows = session.execute(
        select(Article, Analysis, Client)
        .join(Analysis, Analysis.article_id == Article.id)
        .join(Client, Analysis.client_id == Client.id)
        .where(*conditions)
        .order_by(Analysis.importance_score.desc(), Article.published_at.desc())
        .limit(_MAX_CONTEXT_ITEMS)
    ).all()

    lines = [
        f"[{i}] {article.published_at.astimezone(_local_tz()):%d.%m.} "
        f"({article.source}, {client.name}, {analysis.category.value}"
        f"{', ALARM' if analysis.is_alert else ''}): {article.title}"
        + (f" — {analysis.summary}" if analysis.summary else "")
        for i, (article, analysis, client) in enumerate(rows)
    ]
    return " · ".join(label_parts), "\n".join(lines) or "(keine Berichterstattung)"


def _parse_history(raw: str | None) -> list[tuple[str, str]]:
    """The prior turns, as ``(role, text)``. Malformed input yields no history.

    The transcript lives in the browser and is replayed on each turn, because
    ``claude -p`` is stateless per call. A bad payload must degrade to a
    one-shot answer rather than failing the request — losing continuity is a
    smaller harm than losing the answer.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    turns = [
        (str(item.get("role", ""))[:16], str(item.get("text", "")))
        for item in parsed
        if isinstance(item, dict) and item.get("text")
    ]
    return turns[-_MAX_HISTORY_TURNS * 2 :]


def _render_history(turns: list[tuple[str, str]]) -> str:
    lines: list[str] = []
    budget = _MAX_HISTORY_CHARS
    # Newest first while trimming, so the most recent exchange always survives.
    for role, text in reversed(turns):
        speaker = "BERATERIN" if role == "user" else "CAPTAIN COMMS"
        entry = f"{speaker}: {text}"
        if len(entry) > budget:
            break
        budget -= len(entry)
        lines.append(entry)
    return "\n\n".join(reversed(lines))


def _build_prompt(
    question: str, label: str, coverage: str, history: list[tuple[str, str]]
) -> str:
    parts = [_SYSTEM_FRAME, "", f"KONTEXT ({label})", coverage]
    if history:
        parts += ["", "BISHERIGES GESPRÄCH", _render_history(history)]
    parts += ["", "FRAGE", question]
    return "\n".join(parts)


@router.get("/api/assistant/stream")
def assistant_stream(
    request: Request,
    q: str = "",
    history: str | None = None,
    client_id: int | None = None,
    date: str | None = None,
    session: Session = Depends(get_db),
) -> StreamingResponse:
    """Stream an answer to ``q`` about the coverage the reader is looking at."""
    question = (q or "").strip()[:_MAX_QUESTION_CHARS]

    def _events():
        if not question:
            yield StreamEvent("error", "Keine Frage gestellt.").to_sse()
            return
        label, coverage = _coverage_lines(
            session, client_id=client_id, day=_parse_day(date) if date else None
        )
        yield StreamEvent("status", f"Kontext: {label}").to_sse()
        prompt = _build_prompt(question, label, coverage, _parse_history(history))
        for event in stream_claude(prompt):
            yield event.to_sse()

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Proxies that buffer would defeat the entire point of streaming.
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
