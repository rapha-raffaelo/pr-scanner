"""The one list behind the Texte tab.

Impulses and monthly reports were two tabs and read as two products. They are the
same act at two rhythms: RauteOS drafts, the consultant keeps, changes or
discards, and what is left is what gets handed over. Reported as "irgendwie ist
das ganz schön überlappend".

So they share one rail and one tab. The pages behind the entries are still two,
because an occasion and a period are genuinely different objects — one is
something that happened this morning, the other is a month that has ended — and
collapsing the objects would buy a tidy menu with a muddled model. What is
consolidated is the choosing: one place, one row, everything this mandate has to
hand over.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import angles as angles_mod
from .. import report as reports
from ..config import local_zone
from ..models import Client, Report

#: How many months back the rail offers a period that has no report yet. One:
#: the month that has ended is the one a report is drafted for, and offering a
#: year of empty months would bury the entries that exist behind ones that do
#: not.
_EMPTY_PERIODS = 1


@dataclass(frozen=True, slots=True)
class Entry:
    """One thing this mandate has to hand over, occasion or period."""

    kind: str
    key: str
    label: str
    when: str
    href: str
    note: str
    active: bool = False


def _period_key(start: dt.datetime) -> str:
    """A period as the picker names it: ``2026-07``.

    In the reader's zone, never in UTC. A German month starts at 22:00 UTC the
    evening before, so formatting the stored instant directly names the month
    before the one the report is about — the rail said "2026-06" for July.
    """
    return f"{start.astimezone(local_zone()):%Y-%m}"


def _occasion_label(angle) -> str:
    """The occasion's headline, or the first sentence of its text.

    An impulse always has a message and not always a subject, and a rail entry
    reading "Impuls 3" tells the reader nothing about which one it is.
    """
    subject = (getattr(angle, "subject", "") or "").strip()
    if subject:
        return subject
    text = (getattr(angle, "message", "") or "").strip()
    first = text.split(". ")[0].strip()
    return (first[:70] + "…") if len(first) > 70 else (first or "Impuls")


def rail(session: Session, client: Client, *, active: str = "") -> list[Entry]:
    """Everything on offer for this mandate, newest first, occasions and periods.

    ``active`` is the key of the entry the page is showing, so the rail can mark
    it without the caller having to know how a key is spelled.
    """
    entries: list[Entry] = []

    for angle in angles_mod.for_client(session, client.id, limit=None):
        key = f"anlass-{angle.id}"
        entries.append(
            Entry(
                kind="anlass",
                key=key,
                label=_occasion_label(angle),
                when=angle.generated_at.strftime("%d.%m."),
                href=f"/client/{client.id}/advice?eintrag={key}",
                note="",
                active=key == active,
            )
        )

    stored = session.scalars(
        select(Report)
        .where(Report.client_id == client.id)
        .order_by(Report.period_start.desc())
    ).all()
    have = set()
    for row in stored:
        period = _period_key(row.period_start)
        have.add(period)
        key = f"zeitraum-{period}"
        entries.append(
            Entry(
                kind="zeitraum",
                key=key,
                label="Bericht",
                when=period,
                href=f"/client/{client.id}/berichte?zeitraum={period}",
                note="freigegeben" if reports.is_released(row) else "Entwurf",
                active=key == active,
            )
        )

    # The month that has ended and has no report yet: the one entry that is an
    # invitation rather than a thing, and the only way to reach the draft button
    # once the rail replaces the period dropdown as the way in.
    period = reports.previous_month(dt.datetime.now(dt.UTC))
    key_text = _period_key(period.start)
    if key_text not in have:
        key = f"zeitraum-{key_text}"
        entries.append(
            Entry(
                kind="zeitraum",
                key=key,
                label="Bericht",
                when=key_text,
                href=f"/client/{client.id}/berichte?zeitraum={key_text}",
                note="noch nicht erzeugt",
                active=key == active,
            )
        )

    return entries
