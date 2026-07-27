"""The morning digest: one email that says what the day looks like.

``notify.py`` fires only when an alert clears the threshold — correct for
urgency, but it means a quiet-but-not-empty morning sends nothing, and the
operator has to open the dashboard to find that out. The digest is the other
half: a short, scheduled "here is your day" that goes out whether or not
anything fired, so its absence is a signal that the run itself failed.

Deliberately plain text and deliberately short. It is read on a phone before the
laptop is open, and its only job is to answer "do I need to look now?".
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Analysis, Article, Client
from .notify import SmtpConfig, _send_email
from .schemas import Analysis as _AnalysisSchema  # noqa: F401  (keeps schema import parity)
from .stories import cluster

_log = logging.getLogger(__name__)

_MIN_RELEVANCE = 1


@dataclass(frozen=True, slots=True)
class DigestLine:
    """One client's line in the digest."""

    client: str
    stories: int
    alerts: int
    top_headline: str | None


@dataclass(frozen=True, slots=True)
class Digest:
    """The whole message, ready to send."""

    subject: str
    body: str
    total_stories: int
    total_alerts: int


@dataclass(frozen=True, slots=True)
class _Row:
    """Minimal shape the clusterer needs."""

    headline: str
    source: str
    importance: int


def build_digest(
    session: Session, *, day: dt.date | None = None, now: dt.datetime | None = None
) -> Digest:
    """Assemble the digest for ``day`` (default: today, local).

    Counts *stories*, not articles: a syndicated event is one thing to read, and
    a digest that says "12 Artikel" when it is really three stories overstates
    the morning.
    """
    reference = now or dt.datetime.now().astimezone()
    target = day or reference.date()
    tz = reference.tzinfo or dt.UTC
    start = dt.datetime.combine(target, dt.time.min, tzinfo=tz).astimezone(dt.UTC)
    end = start + dt.timedelta(days=1)

    rows = session.execute(
        select(Client.name, Article.title, Article.source, Analysis.importance_score,
               Analysis.is_alert)
        .join(Analysis, Analysis.client_id == Client.id)
        .join(Article, Article.id == Analysis.article_id)
        .where(
            Analysis.relevance_score >= _MIN_RELEVANCE,
            Article.published_at >= start,
            Article.published_at < end,
            Client.is_competitor.is_(False),
        )
        .order_by(Analysis.importance_score.desc())
    ).all()

    by_client: dict[str, list] = {}
    for name, title, source, importance, is_alert in rows:
        by_client.setdefault(name, []).append((title, source, importance, is_alert))

    lines: list[DigestLine] = []
    for name, entries in by_client.items():
        stories = cluster([_Row(t, s, i) for t, s, i, _ in entries])
        lines.append(
            DigestLine(
                client=name,
                stories=len(stories),
                alerts=sum(1 for *_, a in entries if a),
                top_headline=entries[0][0] if entries else None,
            )
        )
    lines.sort(key=lambda line: (line.alerts, line.stories), reverse=True)

    total_stories = sum(line.stories for line in lines)
    total_alerts = sum(line.alerts for line in lines)

    if not lines:
        body = f"Keine Berichterstattung am {target:%d.%m.%Y}."
    else:
        parts = [f"NewsPulse — {target:%d.%m.%Y}", ""]
        for line in lines:
            flag = f", {line.alerts} Alert(s)" if line.alerts else ""
            parts.append(f"{line.client}: {line.stories} Story(s){flag}")
            if line.top_headline:
                parts.append(f"    {line.top_headline}")
        parts += ["", "Vollständig: http://127.0.0.1:8000/"]
        body = "\n".join(parts)

    subject = (
        f"NewsPulse {target:%d.%m.}: {total_alerts} Alert(s), {total_stories} Story(s)"
    )
    return Digest(subject, body, total_stories, total_alerts)


def send_digest(
    session: Session,
    *,
    day: dt.date | None = None,
    smtp: SmtpConfig | None = None,
    send=_send_email,
) -> Digest | None:
    """Build and email the digest. Returns the digest, or ``None`` if unsent.

    Unsent means unconfigured SMTP — logged as a warning, never an exception:
    the digest is a convenience and must not be able to fail a scheduled run.
    """
    digest = build_digest(session, day=day)
    resolved = smtp or SmtpConfig.from_env()
    if resolved is None:
        _log.warning("digest not sent: SMTP is not configured")
        return None
    try:
        send(_DigestSummary(digest.subject, digest.body), resolved)
    except Exception as exc:  # noqa: BLE001 — delivery must never fail the caller
        _log.error("digest delivery failed: %s", exc)
        return None
    return digest


@dataclass(frozen=True, slots=True)
class _DigestSummary:
    """The (subject, body) shape ``notify._send_email`` expects."""

    subject: str
    body: str


__all__ = ["Digest", "DigestLine", "build_digest", "send_digest"]
