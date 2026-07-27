"""Reporting: share of voice, and the Excel deliverable.

An agency's actual output is a document. Until now nothing could leave the tool,
so the monthly client report meant re-typing what the dashboard already knew.

Two things live here:

* :func:`share_of_voice` — how much of the month's conversation each monitored
  company owned. A competitor is a ``Client`` carrying ``is_competitor``: it is
  matched, analysed and archived exactly like a mandate, but never reported *to*.
  Modelling it as a flag rather than a second table means competitor coverage
  gets every capability mandate coverage has, for free.
* :func:`client_workbook` — one client's coverage as an .xlsx, built with the
  pandas/openpyxl pair already in the dependency set.

Both read the same relevance gate as the dashboard, so a number in a report and
the same number on screen can never disagree.
"""

from __future__ import annotations

import datetime as dt
import io
from dataclasses import dataclass

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Analysis, Article, Client

# Same gate the dashboard applies: relevance 0 means "does not concern this
# client", so it is not coverage and must never reach a client-facing number.
_MIN_RELEVANCE = 1


@dataclass(frozen=True, slots=True)
class VoiceShare:
    """One company's slice of the monitored conversation."""

    client_id: int
    name: str
    is_competitor: bool
    mentions: int
    alerts: int
    share: float  # 0..1 of all mentions in the window


def _window(days: int, now: dt.datetime | None = None) -> dt.datetime:
    return (now or dt.datetime.now(dt.UTC)) - dt.timedelta(days=days)


def share_of_voice(
    session: Session, *, days: int = 30, now: dt.datetime | None = None
) -> list[VoiceShare]:
    """Mentions per monitored company over the last ``days``, mandates and
    competitors together, ordered by volume.

    Share is computed over the monitored set only. It is explicitly *not* a
    claim about the whole German media landscape — it answers "of the coverage
    this tool watches, how much was ours", which is the question a portfolio
    review actually asks.
    """
    since = _window(days, now)
    rows = session.execute(
        select(
            Client.id,
            Client.name,
            Client.is_competitor,
            func.count(Analysis.id),
            func.sum(func.coalesce(Analysis.is_alert, 0)),
        )
        .join(Analysis, Analysis.client_id == Client.id)
        .join(Article, Article.id == Analysis.article_id)
        .where(
            Analysis.relevance_score >= _MIN_RELEVANCE,
            Article.published_at >= since,
        )
        .group_by(Client.id, Client.name, Client.is_competitor)
    ).all()

    total = sum(row[3] for row in rows) or 1  # guard the division, not the data
    shares = [
        VoiceShare(
            client_id=row[0],
            name=row[1],
            is_competitor=bool(row[2]),
            mentions=row[3],
            alerts=int(row[4] or 0),
            share=row[3] / total,
        )
        for row in rows
    ]
    return sorted(shares, key=lambda v: v.mentions, reverse=True)


def _coverage_frame(
    session: Session, client_id: int, *, days: int, now: dt.datetime | None = None
) -> pd.DataFrame:
    """One client's coverage in the window as a DataFrame, newest first."""
    since = _window(days, now)
    rows = session.execute(
        select(Article, Analysis)
        .join(Analysis, Analysis.article_id == Article.id)
        .where(
            Analysis.client_id == client_id,
            Analysis.relevance_score >= _MIN_RELEVANCE,
            Article.published_at >= since,
        )
        .order_by(Article.published_at.desc())
    ).all()
    return pd.DataFrame(
        [
            {
                "Datum": article.published_at.astimezone().strftime("%d.%m.%Y %H:%M"),
                "Publisher": article.source,
                "Autor": article.author or "",
                "Schlagzeile": article.title,
                "Zusammenfassung": analysis.summary or "",
                "Kategorie": analysis.category.value,
                "Wichtigkeit": analysis.importance_score,
                "Alarm": "ja" if analysis.is_alert else "",
                "Status": analysis.triage_state.value,
                "Link": article.url,
            }
            for article, analysis in rows
        ],
        # Explicit columns so an empty month still produces a correctly-shaped
        # sheet rather than a blank file the recipient cannot read.
        columns=[
            "Datum", "Publisher", "Autor", "Schlagzeile", "Zusammenfassung",
            "Kategorie", "Wichtigkeit", "Alarm", "Status", "Link",
        ],
    )


def client_workbook(
    session: Session, client: Client, *, days: int = 30, now: dt.datetime | None = None
) -> bytes:
    """One client's coverage as an .xlsx: the report sheet plus a summary.

    Returned as bytes rather than written to a path so the caller decides where
    it goes — a download response, an email attachment, or a file.
    """
    frame = _coverage_frame(session, client.id, days=days, now=now)

    by_category = (
        frame.groupby("Kategorie").size().reset_index(name="Artikel")
        if not frame.empty
        else pd.DataFrame(columns=["Kategorie", "Artikel"])
    )
    by_publisher = (
        frame.groupby("Publisher").size().reset_index(name="Artikel")
        .sort_values("Artikel", ascending=False)
        if not frame.empty
        else pd.DataFrame(columns=["Publisher", "Artikel"])
    )

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name="Berichterstattung", index=False)
        by_category.to_excel(writer, sheet_name="Nach Kategorie", index=False)
        by_publisher.to_excel(writer, sheet_name="Nach Publisher", index=False)
        # Widen the text columns; the default width truncates every headline and
        # makes the sheet unusable without manual resizing.
        sheet = writer.sheets["Berichterstattung"]
        for column, width in (("A", 17), ("B", 22), ("C", 20), ("D", 70),
                              ("E", 70), ("F", 14), ("G", 12), ("H", 8),
                              ("I", 11), ("J", 45)):
            sheet.column_dimensions[column].width = width
    return buffer.getvalue()


__all__ = ["VoiceShare", "client_workbook", "share_of_voice"]
