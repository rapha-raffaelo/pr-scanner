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
from sqlalchemy import Integer, cast, func, select
from sqlalchemy.orm import Session

from . import angles, config, coverage_map
from .models import Analysis, Article, Client, visible_coverage



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
    session: Session,
    client: Client,
    *,
    days: int = 30,
    now: dt.datetime | None = None,
) -> list[VoiceShare]:
    """``client`` and its own competitors by mention volume over ``days``.

    Scoped to one client's comparison set, never the whole portfolio: share of
    voice is a statement about a *market*. Computed across unrelated mandates it
    would produce a number like "Zalando holds 60% versus Siemens", which is not
    a fact about anything.

    The client is always included, even at zero mentions — a quiet month is a
    finding, and dropping the row would make the comparison look like it was
    never run. Competitors with no coverage appear at zero for the same reason.
    """
    since = _window(days, now)
    members = [client, *client.competitors]
    by_id = {member.id: member for member in members}

    counted = dict.fromkeys(by_id, (0, 0))
    rows = session.execute(
        select(
            Analysis.client_id,
            func.count(Analysis.id),
            # Cast before summing. Without it SQLAlchemy applies the Boolean
            # column's result processor to the SUM, so a total of 40 comes back
            # as ``True`` and ``int(True)`` is 1 — the alert column of every
            # share-of-voice table, on screen and in the client report, could
            # never read higher than one.
            func.sum(cast(func.coalesce(Analysis.is_alert, 0), Integer)),
        )
        .join(Article, Article.id == Analysis.article_id)
        .where(
            Analysis.client_id.in_(by_id),
            visible_coverage(),
            Article.published_at >= since,
        )
        .group_by(Analysis.client_id)
    ).all()
    for client_id, mentions, alerts in rows:
        counted[client_id] = (mentions, int(alerts or 0))

    total = sum(mentions for mentions, _ in counted.values())
    shares = [
        VoiceShare(
            client_id=member.id,
            name=member.name,
            # Relative to the client the comparison is *about*, not the global
            # flag: a mandate can be someone else's benchmark.
            is_competitor=member.id != client.id,
            mentions=counted[member.id][0],
            alerts=counted[member.id][1],
            # No coverage at all is 0%, not a division error.
            share=(counted[member.id][0] / total) if total else 0.0,
        )
        for member in members
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
            visible_coverage(),
            Article.published_at >= since,
        )
        .order_by(Article.published_at.desc())
    ).all()
    return pd.DataFrame(
        [
            {
                # Stored UTC; the export reads as a German report, so the column
                # shows the reader's zone rather than the server's.
                "Datum": article.published_at.astimezone(config.local_zone()).strftime(
                    "%d.%m.%Y %H:%M"
                ),
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


def _voice_frame(
    session: Session, client: Client, *, days: int, now: dt.datetime | None
) -> pd.DataFrame:
    """Share of voice as a sheet: who owned how much of the conversation."""
    shares = share_of_voice(session, client, days=days, now=now)
    return pd.DataFrame(
        [
            {
                "Unternehmen": share.name,
                "Rolle": "Wettbewerber" if share.is_competitor else "Mandant",
                "Meldungen": share.mentions,
                "Alerts": share.alerts,
                "Anteil": round(share.share * 100, 1),
            }
            for share in shares
        ],
        columns=["Unternehmen", "Rolle", "Meldungen", "Alerts", "Anteil"],
    )


def _gap_frame(
    session: Session, client: Client, *, days: int, now: dt.datetime | None
) -> pd.DataFrame:
    """The pitch list: outlets covering the competition and never this client."""
    grid = coverage_map.build(session, client, days=days, now=now)
    return pd.DataFrame(
        [
            {
                "Medium": row.source,
                "Über den Wettbewerb": row.rival_articles,
                "Über den Mandanten": row.client_articles,
                "Wer dort vorkommt": ", ".join(
                    f"{cell.company} ({cell.articles})"
                    for cell in row.cells
                    if cell.articles
                ),
            }
            for row in grid.gaps
        ],
        columns=[
            "Medium", "Über den Wettbewerb", "Über den Mandanten", "Wer dort vorkommt",
        ],
    )


def _impulse_frame(session: Session, client: Client, *, days: int) -> pd.DataFrame:
    """The positioning drafts, so the argument travels with the numbers."""
    drafts = angles.for_client(session, client.id)
    return pd.DataFrame(
        [
            {
                "Erzeugt": draft.generated_at.astimezone(config.local_zone()).strftime(
                    "%d.%m.%Y"
                ),
                "Betreff": draft.subject or "",
                "Nachricht": draft.message or "",
                "These": draft.thesis or "",
                "Nicht die These": getattr(draft, "overclaim", "") or "",
            }
            for draft in drafts
        ],
        columns=["Erzeugt", "Betreff", "Nachricht", "These", "Nicht die These"],
    )


def client_workbook(
    session: Session, client: Client, *, days: int = 30, now: dt.datetime | None = None
) -> bytes:
    """One client's month as an .xlsx — the document that goes into the meeting.

    It used to be three sheets of raw coverage: an article list and two pivots.
    A practitioner's verdict on that was exact — "eine Monitoring-Anlage für einen
    Bericht, nicht der Bericht selbst" — because the three things a client
    actually asks about live on three different screens and left the tool only as
    screenshots: how are we doing versus the competition, where should we be and
    are not, and what should we say. Those are now sheets, in that order, ahead of
    the raw list they are drawn from.

    Every sheet reads the same relevance gate as the dashboard, so a number in
    the meeting and the same number on screen cannot disagree.

    Returned as bytes rather than written to a path so the caller decides where
    it goes — a download response, an email attachment, or a file.
    """
    frame = _coverage_frame(session, client.id, days=days, now=now)
    voice = _voice_frame(session, client, days=days, now=now)
    gaps = _gap_frame(session, client, days=days, now=now)
    impulses = _impulse_frame(session, client, days=days)

    reference = now or dt.datetime.now(dt.UTC)
    summary = pd.DataFrame(
        [
            {"Kennzahl": "Mandant", "Wert": client.name},
            {"Kennzahl": "Zeitraum", "Wert": f"letzte {days} Tage"},
            {
                "Kennzahl": "Stand",
                "Wert": reference.astimezone(config.local_zone()).strftime(
                    "%d.%m.%Y %H:%M"
                ),
            },
            {"Kennzahl": "Meldungen", "Wert": len(frame)},
            {
                "Kennzahl": "davon Alerts",
                "Wert": int((frame["Alarm"] == "ja").sum()) if not frame.empty else 0,
            },
            {
                "Kennzahl": "Anteil am Marktgespräch",
                "Wert": (
                    f"{voice.loc[voice['Rolle'] == 'Mandant', 'Anteil'].iloc[0]} %"
                    if not voice.empty and (voice["Rolle"] == "Mandant").any()
                    else "—"
                ),
            },
            {"Kennzahl": "Medien ohne Kontakt (Pitch-Lücken)", "Wert": len(gaps)},
            {"Kennzahl": "Vorliegende Impulse", "Wert": len(impulses)},
        ],
        columns=["Kennzahl", "Wert"],
    )

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
        # Order is the argument: the answer first, the evidence behind it.
        summary.to_excel(writer, sheet_name="Überblick", index=False)
        voice.to_excel(writer, sheet_name="Share of Voice", index=False)
        gaps.to_excel(writer, sheet_name="Pitch-Lücken", index=False)
        impulses.to_excel(writer, sheet_name="Impulse", index=False)
        frame.to_excel(writer, sheet_name="Berichterstattung", index=False)
        by_category.to_excel(writer, sheet_name="Nach Kategorie", index=False)
        by_publisher.to_excel(writer, sheet_name="Nach Publisher", index=False)
        # Widen the text columns; the default width truncates every headline and
        # makes the sheet unusable without manual resizing.
        for name, widths in (
            ("Überblick", (("A", 34), ("B", 30))),
            ("Share of Voice", (("A", 26), ("B", 14), ("C", 12), ("D", 9), ("E", 9))),
            ("Pitch-Lücken", (("A", 34), ("B", 20), ("C", 20), ("D", 52))),
            ("Impulse", (("A", 12), ("B", 60), ("C", 90), ("D", 70), ("E", 50))),
            (
                "Berichterstattung",
                (("A", 17), ("B", 22), ("C", 20), ("D", 70), ("E", 70), ("F", 14),
                 ("G", 12), ("H", 8), ("I", 11), ("J", 45)),
            ),
        ):
            sheet = writer.sheets[name]
            for column, width in widths:
                sheet.column_dimensions[column].width = width
    return buffer.getvalue()


__all__ = ["VoiceShare", "client_workbook", "share_of_voice"]
