"""Reporting: share of voice, the Excel deliverable, and the figures a report may make.

An agency's actual output is a document. Until now nothing could leave the tool,
so the monthly client report meant re-typing what the dashboard already knew.

Three things live here:

* :func:`share_of_voice` — how much of the month's conversation each monitored
  company owned. A competitor is a ``Client`` carrying ``is_competitor``: it is
  matched, analysed and archived exactly like a mandate, but never reported *to*.
  Modelling it as a flag rather than a second table means competitor coverage
  gets every capability mandate coverage has, for free.
* :func:`client_workbook` — one client's coverage as an .xlsx, built with the
  pandas/openpyxl pair already in the dependency set.
* The measurement layer — :func:`period_metrics`, :func:`attributed_coverage`
  and :func:`message_pull_through`. Every one of them hands back its figure
  *together with the rows it was computed from*, so a claim in a report cites
  evidence because the metric supplied it, not because a model remembered where
  a number came from.

All of them read the same relevance gate as the dashboard, so a number in a
report and the same number on screen can never disagree.

What may not be measured
------------------------
Reach, impressions and advertising equivalence are the standard filler of PR
reporting, and RauteOS holds none of the data they need. A report that estimated
them would be the first thing here a client could catch out, so they are not
printed with a caveat: they cannot be produced at all. :data:`MetricKey` is the
closed set of figures that exist, :data:`FORBIDDEN_FIGURES` names what is out of
bounds, and :class:`MetricValue` refuses to be built with anything else — a guard
in code rather than a sentence in a prompt, because a prompt can be reworded.
"""

from __future__ import annotations

import datetime as dt
import io
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

import pandas as pd
from sqlalchemy import Integer, cast, func, select
from sqlalchemy.orm import Session

from . import angles, coach, config, coverage_map, matching, outlets, outreach
from .models import Analysis, Article, Client, Outreach, Tonality, visible_coverage



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


# --- The measurement layer ------------------------------------------------------


class MetricKey(StrEnum):
    """Every figure a report may print, and the complete list of them.

    Closed on purpose. A finding may cite a figure only if it is one of these, so
    the set is the boundary between what RauteOS measured and what somebody wished
    it had measured.
    """

    COVERAGE = "berichterstattung"
    SHARE_OF_VOICE = "share_of_voice"
    TONALITY = "tonalitaet"
    LEAD_MEDIA = "leitmedien"
    ATTRIBUTED = "eigene_ansprache"
    MESSAGE = "botschaft"


#: What each key is called in the document. Derived from the key rather than
#: passed in, so no caller can label a permitted figure as a forbidden one.
_LABELS: dict[MetricKey, str] = {
    MetricKey.COVERAGE: "Beiträge",
    MetricKey.SHARE_OF_VOICE: "Anteil am Marktgespräch",
    MetricKey.TONALITY: "Tonalität",
    MetricKey.LEAD_MEDIA: "Beiträge in Leitmedien",
    MetricKey.ATTRIBUTED: "Aus eigener Ansprache",
    MetricKey.MESSAGE: "Kernbotschaft",
}

#: The figures RauteOS may not produce, in the words they are usually asked for.
#: None of them is derivable from an archive of headlines and a ledger of letters,
#: so each would be an estimate wearing a fact's clothes. Kept as data so a later
#: caller — a prompt, a template, a route — can be checked against it.
FORBIDDEN_FIGURES: frozenset[str] = frozenset(
    {
        "reichweite",
        "impressionen",
        "impressions",
        "kontaktchancen",
        "opportunities to see",
        "werbewert",
        "werbeäquivalenz",
        "anzeigenäquivalenz",
        "advertising value",
        "media value",
        "auflage",
        "leserschaft",
        "visits",
        "unique user",
        # Short terms, matched as whole words — see _MIN_SUBSTRING_TERM.
        "ave",
        "ots",
        "reach",
    }
)

#: A forbidden term shorter than this is matched as a whole word; any longer one is
#: matched anywhere in the name, so "geschätzte Reichweite" is the same refusal as
#: "Reichweite". The split is what keeps "ave" from refusing "Average" and "reach"
#: from refusing "Outreach", which is a word this product uses for something else
#: entirely.
_MIN_SUBSTRING_TERM = 6

#: Below this a difference between two shares is float noise, not a movement.
_DIRECTION_EPSILON = 1e-9

#: The tier ``outlet_tiers.toml`` calls Leitmedien. Coverage there sets the agenda
#: and the rest follows it, which is why it is counted separately at all.
LEAD_MEDIA_TIER = 1

#: How long a released letter may be credited with a piece of coverage. Thirty days
#: is the interval the outreach spec already uses for "this pitch produced this
#: byline"; beyond it the connection is a coincidence with a date on it.
ATTRIBUTION_WINDOW_DAYS = 30


class ForbiddenFigure(ValueError):
    """An attempt to produce a figure this tool is not allowed to state."""


class Direction(StrEnum):
    """How a figure moved against the same length of period before it."""

    UP = "gestiegen"
    DOWN = "gefallen"
    FLAT = "unverändert"
    #: No comparable period in the archive. Not the same as "unchanged", and a
    #: report that showed the two alike would be inventing a comparison.
    UNKNOWN = "unbekannt"


def is_forbidden(name: str) -> bool:
    """Whether ``name`` names a figure RauteOS may not produce.

    Substring matching for the words, whole-word matching for the acronyms:
    "geschätzte Reichweite" is the same refusal as "Reichweite", while "AVE" must
    not refuse "Average". A false positive costs a figure that has to be renamed; a
    false negative costs a number in a client's document that the agency cannot
    defend from its own data.
    """
    lowered = " ".join((name or "").casefold().split())
    words = set(re.findall(r"\w+", lowered, re.UNICODE))
    return any(
        term in lowered if len(term) >= _MIN_SUBSTRING_TERM else term in words
        for term in FORBIDDEN_FIGURES
    )


@dataclass(frozen=True, slots=True)
class Period:
    """A reporting window: ``start`` inclusive, ``end`` exclusive, both UTC."""

    start: dt.datetime
    end: dt.datetime

    def __post_init__(self) -> None:
        for name in ("start", "end"):
            value = getattr(self, name)
            if value.tzinfo is None:
                object.__setattr__(self, name, value.replace(tzinfo=dt.UTC))
        if self.end <= self.start:
            raise ValueError("a reporting period ends after it starts")

    @property
    def length(self) -> dt.timedelta:
        return self.end - self.start

    @property
    def previous(self) -> Period:
        """The window of the same length immediately before this one.

        The same *length*, never "the previous calendar month": a report pulled on
        the 12th covers twelve days, and comparing those twelve against a full
        month would show a collapse in coverage that only happened in the arithmetic.
        """
        return Period(self.start - self.length, self.start)

    @classmethod
    def month(cls, year: int, month: int) -> Period:
        """One calendar month, bounded by local midnight and stored as UTC.

        The reader's month, not the server's: a piece published at 00:30 Berlin
        time on the first belongs to the month a client would put it in.
        """
        zone = config.local_zone()
        start = dt.datetime(year, month, 1, tzinfo=zone)
        following = dt.datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=zone)
        return cls(start.astimezone(dt.UTC), following.astimezone(dt.UTC))

    @classmethod
    def ending(cls, end: dt.datetime, *, days: int) -> Period:
        """The ``days`` before ``end`` — the shape the dashboard windows use."""
        return cls(end - dt.timedelta(days=days), end)


@dataclass(frozen=True, slots=True)
class MetricValue:
    """One figure, what it was before, and the rows it was computed from.

    The ids are the point. A finding in a report cites evidence because the metric
    handed the evidence over with the number, so "drei der 24 Beiträge gehen auf
    eigene Ansprache zurück" can be re-derived — or contradicted — by opening the
    rows named here, without asking the model that wrote the sentence.

    ``figure`` is ``None`` when there is no figure to state, and then ``note`` says
    why. That distinction is load-bearing: a month with no coverage and a month
    with no positive coverage are different statements, and a zero in place of the
    first is a claim nobody checked.
    """

    key: MetricKey
    client_id: int
    figure: float | None
    previous: float | None = None
    #: Why there is no figure, or a qualification the reader needs with it.
    note: str = ""
    #: Which tonality, which key message. Client text, never a figure name.
    subject: str = ""
    analysis_ids: tuple[int, ...] = ()
    outreach_ids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        # The guard. A figure exists only if it is in the closed set, so a future
        # prompt, route or template cannot introduce reach by wording: there is no
        # key to put it under and no free-text label to hide it in.
        if not isinstance(self.key, MetricKey):
            raise ForbiddenFigure(
                f"{self.key!r} is not a figure this tool produces; "
                f"permitted: {', '.join(sorted(MetricKey))}"
            )
        # The key is not the only thing that reaches the page. Both free-text
        # fields travel into the document beside the number, so a figure labelled
        # COVERAGE and captioned "Geschätzte Reichweite" would state the forbidden
        # figure with the guard intact. They are checked against the same list.
        if is_forbidden(self.note):
            raise ForbiddenFigure(f"a metric's note may not name {self.note!r}")
        # A key message is the mandate's own wording quoted back at it: a guide
        # whose message is "Reichweite in der Zielgruppe" is a sentence the client
        # wrote, not a figure this tool produced. Every other subject is ours.
        if self.key is not MetricKey.MESSAGE and is_forbidden(self.subject):
            raise ForbiddenFigure(f"a metric's subject may not name {self.subject!r}")
        if self.figure is None and not self.note.strip():
            raise ValueError("a missing figure has to say why it is missing")

    @property
    def label(self) -> str:
        """What this figure is called in the document."""
        return _LABELS[self.key]

    @property
    def direction(self) -> Direction:
        if self.figure is None or self.previous is None:
            return Direction.UNKNOWN
        if self.figure - self.previous > _DIRECTION_EPSILON:
            return Direction.UP
        if self.previous - self.figure > _DIRECTION_EPSILON:
            return Direction.DOWN
        return Direction.FLAT


@dataclass(frozen=True, slots=True)
class PeriodMetrics:
    """One mandate's month, measured. Every slot is filled, with a figure or a reason."""

    client_id: int
    period: Period
    coverage: MetricValue
    share_of_voice: MetricValue
    lead_media: MetricValue
    tonality: tuple[MetricValue, ...] = ()
    #: The period-level statement, when there is one to make.
    note: str = ""

    @property
    def empty(self) -> bool:
        """Whether the period holds no coverage at all — not "holds zero of something"."""
        return self.coverage.figure is None

    @property
    def values(self) -> tuple[MetricValue, ...]:
        """The *period-level* figures actually produced, and no more.

        Not the whole citable set. :func:`attributed_coverage` and
        :func:`message_pull_through` are separate calls, and what they return is
        just as citable — a finding about coverage "aus eigener Ansprache" is the
        strongest sentence a report can carry, and a whitelist built from this
        property alone would reject it. A caller checking what a finding may cite
        wants the union of all three.
        """
        stated = [self.coverage, self.share_of_voice, self.lead_media, *self.tonality]
        return tuple(value for value in stated if value.figure is not None)


@dataclass(frozen=True, slots=True)
class _Row:
    """One visible piece of coverage, flattened so every metric reads the same rows."""

    analysis_id: int
    client_id: int
    source: str
    tonality: Tonality
    author: str
    title: str
    summary: str
    published_at: dt.datetime

    @property
    def text(self) -> str:
        """The syndicated text a message can be found in. No body is ever stored."""
        return f"{self.title}\n{self.summary}"


def _rows(session: Session, client_ids: Sequence[int], period: Period) -> list[_Row]:
    """Every visible piece of coverage for ``client_ids`` inside ``period``.

    One query, then all counting happens in Python over the rows themselves —
    which is what lets each figure hand back the ids it was computed from instead
    of a number a caller has to take on trust.
    """
    if not client_ids:
        return []
    found = session.execute(
        select(Analysis, Article)
        .join(Article, Article.id == Analysis.article_id)
        .where(
            Analysis.client_id.in_(list(client_ids)),
            visible_coverage(),
            Article.published_at >= period.start,
            Article.published_at < period.end,
        )
        .order_by(Article.published_at)
    ).all()
    return _to_rows(found)


def _rows_by_id(session: Session, analysis_ids: Sequence[int]) -> list[_Row]:
    """The cited rows as they stand *now*, with no period and no client filter.

    A recomputation is allowed to see only the ids the figure named. That is what
    makes it a check on the figure rather than a second computation of it.
    """
    if not analysis_ids:
        return []
    found = session.execute(
        select(Analysis, Article)
        .join(Article, Article.id == Analysis.article_id)
        .where(Analysis.id.in_(list(analysis_ids)), visible_coverage())
        .order_by(Article.published_at)
    ).all()
    return _to_rows(found)


def _to_rows(found: Sequence[tuple[Analysis, Article]]) -> list[_Row]:
    return [
        _Row(
            analysis_id=analysis.id,
            client_id=analysis.client_id,
            source=article.source or "",
            tonality=analysis.tonality,
            author=article.author or "",
            title=article.title or "",
            summary=article.summary_text or "",
            published_at=article.published_at,
        )
        for analysis, article in found
    ]


def _ids(rows: Sequence[_Row]) -> tuple[int, ...]:
    return tuple(row.analysis_id for row in rows)


def _has_coverage_before(session: Session, client_id: int, moment: dt.datetime) -> bool:
    """Whether the archive holds anything for this client before ``moment``.

    The gate on every previous-period figure. Without it a mandate onboarded last
    week would read "Beiträge: 12, vorher 0", which states a collapse that never
    happened — the archive simply did not exist yet.
    """
    return (
        session.scalar(
            select(Analysis.id)
            .join(Article, Article.id == Analysis.article_id)
            .where(
                Analysis.client_id == client_id,
                visible_coverage(),
                Article.published_at < moment,
            )
            .limit(1)
        )
        is not None
    )


def _voice_share(rows: Sequence[_Row], client_id: int) -> float | None:
    """The mandate's part of its own comparison set, 0..1, or ``None`` for silence.

    0/5 is a share. 0/0 is not: a period in which nobody in the comparison set was
    written about at all holds no conversation to own a part of, and stating that
    as "0 % Anteil" turns the next period into a rise out of nothing. The same
    distinction the empty month makes, one level down.
    """
    if not rows:
        return None
    return sum(1 for row in rows if row.client_id == client_id) / len(rows)


def _lead_media_rows(rows: Sequence[_Row]) -> list[_Row]:
    return [row for row in rows if outlets.tier_for(row.source) == LEAD_MEDIA_TIER]


def _tonality_metrics(
    client_id: int,
    rows: Sequence[_Row],
    previous: Sequence[_Row] | None,
) -> tuple[MetricValue, ...]:
    """The split, one figure per tone, each carrying its own rows.

    ``positiv``, ``neutral`` and ``negativ`` are always present so a zero in one of
    them is readable as a zero; ``unbekannt`` appears only when it has rows, since
    it exists for analyses written before tonality did and is not a finding.
    """
    grouped: dict[Tonality, list[_Row]] = {}
    for row in rows:
        grouped.setdefault(row.tonality, []).append(row)
    shown = [
        tone
        for tone in Tonality
        if tone is not Tonality.UNBEKANNT or grouped.get(tone)
    ]
    return tuple(
        MetricValue(
            key=MetricKey.TONALITY,
            client_id=client_id,
            figure=float(len(grouped.get(tone, ()))),
            previous=(
                None
                if previous is None
                else float(sum(1 for row in previous if row.tonality is tone))
            ),
            subject=tone.value,
            analysis_ids=_ids(grouped.get(tone, ())),
        )
        for tone in shown
    )


def period_metrics(session: Session, client: Client, period: Period) -> PeriodMetrics:
    """One mandate's period, measured against the same length of period before it.

    Coverage count, share of voice, the tonality split and coverage in Leitmedien,
    each with the analyses behind it. A period with no coverage returns *empty*
    metrics rather than zeros: "nothing was written about you in July" and "we hold
    no July" are different statements about a month, and only one of them belongs
    in a document with the agency's name on it.
    """
    own = _rows(session, [client.id], period)
    if not own:
        return _empty_metrics(client, period)

    comparable = _has_coverage_before(session, client.id, period.start)
    before = _rows(session, [client.id], period.previous) if comparable else None
    lead = _lead_media_rows(own)
    return PeriodMetrics(
        client_id=client.id,
        period=period,
        coverage=MetricValue(
            key=MetricKey.COVERAGE,
            client_id=client.id,
            figure=float(len(own)),
            previous=None if before is None else float(len(before)),
            analysis_ids=_ids(own),
        ),
        share_of_voice=_voice_metric(session, client, period, comparable=comparable),
        lead_media=MetricValue(
            key=MetricKey.LEAD_MEDIA,
            client_id=client.id,
            figure=float(len(lead)),
            previous=(
                None if before is None else float(len(_lead_media_rows(before)))
            ),
            analysis_ids=_ids(lead),
        ),
        tonality=_tonality_metrics(client.id, own, before),
    )


#: Said once, so every metric that goes silent for the same reason says the same
#: thing. A month with nothing in it is an answer, not a gap in the tool.
_NO_COVERAGE = "Keine Berichterstattung im Zeitraum."


def _empty_metrics(client: Client, period: Period) -> PeriodMetrics:
    """Every figure absent, with the one reason all of them share."""

    def absent(key: MetricKey) -> MetricValue:
        return MetricValue(
            key=key, client_id=client.id, figure=None, note=_NO_COVERAGE
        )

    return PeriodMetrics(
        client_id=client.id,
        period=period,
        coverage=absent(MetricKey.COVERAGE),
        share_of_voice=absent(MetricKey.SHARE_OF_VOICE),
        lead_media=absent(MetricKey.LEAD_MEDIA),
        note=_NO_COVERAGE,
    )


def _voice_metric(
    session: Session, client: Client, period: Period, *, comparable: bool
) -> MetricValue:
    """Share of voice against this mandate's *stored* comparison set.

    Never a set inferred from the industry: a benchmark somebody chose is a
    statement the agency stands behind, and one the tool assembled from a shared
    Branche field is a number about a market nobody defined. A mandate with no
    comparison set therefore gets no share of voice at all, and says so — the
    honest answer, where 100% of a set of one would be a flattering fiction.
    """
    members = [client.id, *(rival.id for rival in client.competitors)]
    if len(members) == 1:
        return MetricValue(
            key=MetricKey.SHARE_OF_VOICE,
            client_id=client.id,
            figure=None,
            note=(
                "Kein Vergleichsumfeld hinterlegt. Ein Anteil am Marktgespräch "
                "braucht die Wettbewerber, die für diesen Mandanten hinterlegt sind."
            ),
        )
    rows = _rows(session, members, period)
    share = _voice_share(rows, client.id)
    if share is None:
        return MetricValue(
            key=MetricKey.SHARE_OF_VOICE,
            client_id=client.id,
            figure=None,
            note=(
                "Im Zeitraum wurde weder über den Mandanten noch über einen der "
                "hinterlegten Wettbewerber geschrieben. Ohne Marktgespräch gibt es "
                "keinen Anteil daran."
            ),
        )
    before = _rows(session, members, period.previous) if comparable else None
    return MetricValue(
        key=MetricKey.SHARE_OF_VOICE,
        client_id=client.id,
        figure=share,
        # ``None`` where the whole comparison set was silent: the gate above only
        # asks whether *this mandate* has an archive, and a set nobody wrote about
        # last month is a second way for the baseline not to exist.
        previous=None if before is None else _voice_share(before, client.id),
        # The whole comparison, not only the mandate's own rows: the denominator is
        # part of the figure, so a reader recomputing it needs both halves.
        analysis_ids=_ids(rows),
    )


def attributed_coverage(
    session: Session,
    client: Client,
    period: Period,
    *,
    window_days: int = ATTRIBUTION_WINDOW_DAYS,
) -> MetricValue:
    """Coverage this agency's own outreach can be shown to have caused.

    A piece counts only where a *released* letter to that journalist precedes it
    inside the attribution window. Three parts of that sentence are load-bearing:
    released, because a draft nobody sent caused nothing; precedes, because a
    letter written after the article is a coincidence; and the window, because a
    pitch does not keep earning credit for a year.

    A piece answering two letters is counted once — it is one piece of coverage —
    while both letters stay in ``outreach_ids``, since both are evidence for it.
    """
    rows = _rows(session, [client.id], period)
    if not rows:
        return MetricValue(
            key=MetricKey.ATTRIBUTED,
            client_id=client.id,
            figure=None,
            note=_NO_COVERAGE,
        )
    window = dt.timedelta(days=window_days)
    letters = outreach.released_letters(
        session, client.id, since=period.start - window, until=period.end
    )
    if not letters:
        return MetricValue(
            key=MetricKey.ATTRIBUTED,
            client_id=client.id,
            figure=None,
            note=(
                "Keine freigegebenen Anschreiben im Zeitraum. Ohne eigene Ansprache "
                "gibt es nichts zuzurechnen — das ist keine Null, sondern keine Frage."
            ),
        )
    matched, letter_ids = _attribute(rows, letters, window)
    previous = (
        _attributed_figure(session, client, period.previous, window)
        if _has_coverage_before(session, client.id, period.start)
        else None
    )
    return MetricValue(
        key=MetricKey.ATTRIBUTED,
        client_id=client.id,
        figure=float(len(matched)),
        previous=previous,
        note=_unnamed_note(letters),
        analysis_ids=_ids(matched),
        outreach_ids=letter_ids,
    )


def _unnamed_note(letters: Sequence[Outreach]) -> str:
    """What letters with no named recipient did to the figure, said out loud.

    A German feed carries a byline about one time in ten, and a pitch addressed to
    an outlet rather than a person is a normal thing to write — but it can never be
    tied to one, so it lowers this figure without appearing in it. Naming how many
    is the difference between a low number that is a result and a low number that
    is a limit of the evidence.
    """
    unnamed = sum(1 for letter in letters if not (letter.journalist or "").strip())
    if not unnamed:
        return ""
    return (
        f"{unnamed} von {len(letters)} freigegebenen Anschreiben gingen an kein "
        "namentliches Postfach und sind deshalb keiner Zeile zurechenbar."
    )


def _attributed_figure(
    session: Session, client: Client, period: Period, window: dt.timedelta
) -> float | None:
    """The same count over an earlier period, for the comparison.

    ``None`` wherever :func:`attributed_coverage` would also have refused a figure
    — no coverage to attribute, or no released letter that could have caused any.
    A zero here would render the same state of the world as "keine Frage" in one
    period and as a hard number in the other, and the difference between the two
    would read as a rise against a month where attribution was never askable.

    Ids are not kept: a report cites the evidence for this month, not the one
    before it.
    """
    rows = _rows(session, [client.id], period)
    if not rows:
        return None
    letters = outreach.released_letters(
        session, client.id, since=period.start - window, until=period.end
    )
    if not letters:
        return None
    matched, _ = _attribute(rows, letters, window)
    return float(len(matched))


def _attribute(
    rows: Sequence[_Row], letters: Sequence[Outreach], window: dt.timedelta
) -> tuple[list[_Row], tuple[int, ...]]:
    """The pieces a released letter preceded, and the letters that earned them.

    The byline is matched with the same word-boundary matcher the whole codebase
    uses for names, so "Von Anna Muster" and "Anna Muster (dpa)" are the journalist
    the letter went to and "Annamaria Musterfrau" is not.
    """
    matched: dict[int, _Row] = {}
    credited: list[int] = []
    for letter in letters:
        journalist = (getattr(letter, "journalist", "") or "").strip()
        matcher = matching.terms_matcher([journalist]) if journalist else None
        if matcher is None:
            # A letter addressed to an outlet rather than a person cannot be tied
            # to a byline, and guessing from the outlet would credit outreach with
            # coverage it never touched.
            continue
        released = letter.released_at
        earns = [
            row
            for row in rows
            if row.author
            and matcher.search(row.author.casefold()) is not None
            and released <= row.published_at < released + window
        ]
        if not earns:
            continue
        credited.append(letter.id)
        for row in earns:
            matched.setdefault(row.analysis_id, row)
    return list(matched.values()), tuple(sorted(credited))


def message_pull_through(
    session: Session, client: Client, period: Period
) -> tuple[MetricValue, ...]:
    """How often each of the guide's own key messages turned up in the coverage.

    One figure per message, each carrying the pieces that carry it, so "die
    Botschaft trägt" is a list of headlines rather than an impression. The matching
    is :mod:`newspulse.coach`'s, which is where the guide is compared against
    coverage — two readings of "the message landed" in one product would be worse
    than either.
    """
    messages = coach.key_messages(client)
    if not messages:
        return (
            MetricValue(
                key=MetricKey.MESSAGE,
                client_id=client.id,
                figure=None,
                note=(
                    "Keine Kernbotschaften im Kommunikations-Guide hinterlegt. "
                    "Ohne sie gibt es nichts, woran sich die Berichterstattung "
                    "messen ließe."
                ),
            ),
        )
    rows = _rows(session, [client.id], period)
    if not rows:
        return (
            MetricValue(
                key=MetricKey.MESSAGE,
                client_id=client.id,
                figure=None,
                note=_NO_COVERAGE,
            ),
        )
    before = (
        _rows(session, [client.id], period.previous)
        if _has_coverage_before(session, client.id, period.start)
        else None
    )
    return tuple(
        _message_metric(client.id, message, rows, before) for message in messages
    )


#: Printed with every pull-through figure, because "die Botschaft trägt" means
#: something specific here and a reader who is told what it means can disagree with
#: the count. Left to the report to render or drop; it is a qualification, not a
#: reason the figure is missing.
_MESSAGE_RULE = (
    "Gezählt wird ein Beitrag, der die Botschaft als Ganzes aufnimmt oder "
    "mindestens zwei ihrer tragenden Begriffe."
)


def _message_metric(
    client_id: int,
    message: str,
    rows: Sequence[_Row],
    before: Sequence[_Row] | None,
) -> MetricValue:
    matcher = coach.message_matcher(message)
    carried = [row for row in rows if coach.carries(row.text, matcher)]
    return MetricValue(
        key=MetricKey.MESSAGE,
        client_id=client_id,
        note=_MESSAGE_RULE,
        figure=float(len(carried)),
        previous=(
            None
            if before is None
            else float(sum(1 for row in before if coach.carries(row.text, matcher)))
        ),
        subject=message,
        analysis_ids=_ids(carried),
    )


def _keeps(metric: MetricValue) -> Callable[[_Row], bool]:
    """The rule that put a row behind ``metric``, rebuilt from the metric itself.

    Re-applied on recomputation rather than assumed. A count of surviving ids
    agrees with itself even when the rule that selected them has drifted — a
    changed outlet tier, a corrected tone, a reworded message — and that drift is
    exactly the failure a reader opening the cited rows could not otherwise see.
    """
    if metric.key is MetricKey.LEAD_MEDIA:
        return lambda row: outlets.tier_for(row.source) == LEAD_MEDIA_TIER
    if metric.key is MetricKey.TONALITY:
        return lambda row: row.tonality.value == metric.subject
    if metric.key is MetricKey.MESSAGE:
        probe = coach.message_matcher(metric.subject)
        return lambda row: coach.carries(row.text, probe)
    # Coverage — and attributed coverage, whose second half is the cited letters
    # rather than these rows — is the count itself: still being visible coverage
    # *is* the rule, and _rows_by_id has already applied it.
    return lambda row: True


def recompute(session: Session, metric: MetricValue) -> float | None:
    """Re-derive a figure from the ids it cited, and from nothing else.

    The check the whole layer rests on: if the number and the evidence disagree,
    the evidence wins. It reads the cited rows as they are *now*, so a figure whose
    articles were since dismissed comes back smaller — which is what lets a report
    notice that the ground under a claim moved instead of restating it — and it
    re-applies the rule that picked them rather than trusting that it still holds.

    Returns ``None`` for a metric that states no figure.
    """
    if metric.figure is None:
        return None
    rows = _rows_by_id(session, metric.analysis_ids)
    if metric.key is MetricKey.SHARE_OF_VOICE:
        # A share whose whole comparison has gone is not a share; 0.0 is returned
        # so the check *disagrees* with the stated figure rather than reporting
        # "no figure", which is what a caller reads as "nothing to check".
        share = _voice_share(rows, metric.client_id)
        return 0.0 if share is None else share
    keeps = _keeps(metric)
    return float(sum(1 for row in rows if keeps(row)))


__all__ = [
    "ATTRIBUTION_WINDOW_DAYS",
    "Direction",
    "FORBIDDEN_FIGURES",
    "ForbiddenFigure",
    "LEAD_MEDIA_TIER",
    "MetricKey",
    "MetricValue",
    "Period",
    "PeriodMetrics",
    "VoiceShare",
    "attributed_coverage",
    "client_workbook",
    "is_forbidden",
    "message_pull_through",
    "period_metrics",
    "recompute",
    "share_of_voice",
]
