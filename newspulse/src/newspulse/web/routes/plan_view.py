"""Der Redaktionsplan: six months of hooks, each resolving to a stored row.

DEC-5 option A — a stack of months, hooks inside them, and an empty month
written out rather than skipped. The engine behind it is :mod:`newspulse.plan`,
which already refuses to store a hook whose evidence does not resolve; this
module is what makes that refusal visible, because a plan whose dates cannot be
checked is a pretty list.

Three properties carry the page, and each of them is a thing an agency would
otherwise get wrong at a retainer meeting.

* **Every hook links to its evidence.** Not to a search, not to a section that
  might contain it: the row the hook was built from, on the mandate's own pages.
  A hook whose row has since been deleted says so and offers no link — which is
  the one honest answer, and the reason :class:`Evidence` is nullable here.
* **An empty month is written out.** It gets a card of its own with the sentence
  saying what is missing and where to fix it, because a month silently absent
  from a six-month plan reads as a month that was never checked.
* **A mandate with nothing to build from gets no plan at all.** Without stored
  themes and without market signals there is no evidence, so the page names the
  two gaps and links to them instead of rendering six empty months, which would
  blame the calendar for an unconfigured mandate.

The document is a second template rather than this one with the links stripped.
The screen page is app furniture — tabs, buttons, a month picker — and a client
receives none of that; giving the artefact its own file means the download can
never carry a control by forgetting a flag.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import threading
from dataclasses import dataclass, replace
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import assets as assets_mod
from ... import config, plan
from ...db import get_session
from ...models import (
    Analysis,
    Angle,
    Article,
    Asset,
    Client,
    HookSource,
    HookState,
    MarketSignal,
    PlanHook,
    SignalKind,
    TopicHit,
)
from ..app import get_db, templates
from ..mandates import mandate_or_404
from ..runlock import SWEEP_RUNNING as _sweep_running
from ..runlock import guard as _run_guard
from .. import spawn
from .today import _fetch_last_run, _local_tz

router = APIRouter()

_log = logging.getLogger(__name__)
_SEE_OTHER = 303

#: The German month names, spelled out here for the same reason ``web.app``
#: spells them out: a de_DE locale is absent from most containers, and
#: ``setlocale`` is process-global.
_MONTHS = (
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
)

#: The three-letter form under the day, as a calendar prints it.
_MONTHS_SHORT = (
    "Jan", "Feb", "Mär", "Apr", "Mai", "Jun",
    "Jul", "Aug", "Sep", "Okt", "Nov", "Dez",
)

#: The filename a download falls back to when the mandate's name survives
#: sanitising as nothing at all — the same fallback the report and the
#: Pressespiegel use, so the three artefacts of one mandate sort together.
_FILENAME_FALLBACK = "mandant"


@dataclass(frozen=True, slots=True)
class Klasse:
    """The Herkunftsklasse of a hook, as the legend names and colours it.

    Five rather than three: the plan's own source classes are
    :class:`~newspulse.models.HookSource`'s three, but a market signal already
    carries a class of its own — a study, a regulatory date and a conference are
    read for different reasons — and flattening all three into "Marktsignal"
    would throw away the distinction the market page was built to make.
    """

    #: The CSS modifier on the tag, and the legend's swatch.
    key: str
    label: str


STUDIE = Klasse("study", "Studie")
REGULIERUNG = Klasse("reg", "Regulierung")
VERANSTALTUNG = Klasse("event", "Veranstaltung")
THEMA = Klasse("thema", "Thema")
ARCHIVMUSTER = Klasse("archiv", "Archivmuster")

#: The legend above the stack, in the order the classes are read in.
KLASSEN = (STUDIE, REGULIERUNG, VERANSTALTUNG, THEMA, ARCHIVMUSTER)

_SIGNAL_KLASSEN = {
    SignalKind.STUDIE: STUDIE,
    SignalKind.REGULIERUNG: REGULIERUNG,
    SignalKind.VERANSTALTUNG: VERANSTALTUNG,
}

#: What a hook whose evidence no longer resolves is called. It keeps its source
#: class name rather than being dropped: the hook was stored against a row that
#: existed, and hiding it would quietly shorten the plan.
_FALLBACK_KLASSEN = {
    HookSource.MARKTSIGNAL: Klasse("", "Marktsignal"),
    HookSource.THEMA: THEMA,
    HookSource.VORJAHR: ARCHIVMUSTER,
}

#: The German name of each format, keyed by what ``Asset.kind`` stores. Built off
#: the registry so a seventh format needs no change here.
_FORMAT_LABELS = {key: fmt.name for key, fmt in assets_mod.REGISTRY.items()}

#: What each state is called on the page. Here rather than in the template, so
#: the words are one list a translator can find and a new state cannot ship as a
#: raw key in front of a reader.
STATE_LABELS = {
    HookState.VORGESCHLAGEN: "Vorgeschlagen",
    HookState.ANGENOMMEN: "Angenommen",
    HookState.VERWORFEN: "Verworfen",
}

#: Said when a recompute is refused because one is already running. In memory and
#: per mandate, the same posture the impulse's refusal takes: it describes one
#: click rather than the mandate, and going stale on a restart is correct.
_BUSY = (
    "Der Plan wird gerade neu berechnet. Der Auftrag wurde nicht angenommen: "
    "warten Sie, bis der laufende steht, sonst wird derselbe Aufruf zweimal "
    "bezahlt."
)
#: What a refused click says when the daily sweep is the one holding the guard.
#: The impulse's package says the same sentence in the same situation, so it is
#: read from beside the guard rather than written out here: two copies of one
#: German string are two keys in ``i18n._EN``, and the second silently overrides
#: the first's English.
_SWEEP_RUNNING = _sweep_running
#: What a crashed recompute says. Static and without the exception text in it, so
#: it is one key a translator can hold — the detail belongs in the log, where the
#: stack trace already is, and a reader cannot act on a Python repr anyway.
_FAILED = (
    "Die Neuberechnung ist mit einem Fehler abgebrochen. Der bisherige Plan "
    "steht unverändert. Details stehen im Log."
)
#: Refused move. Static for the same reason: a month name interpolated into the
#: sentence would make it a key no translation table can carry.
_NOT_A_PLAN_MONTH = (
    "Der gewählte Monat liegt nicht im Plan. Verschoben wird nur innerhalb der "
    "Monate, die der Plan zeigt."
)


# --- Evidence -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Evidence:
    """The stored row a hook came from, as the page cites and links to it.

    ``ref`` names the row the way the plan's own refusal names it — table and id
    — so a reader can say out loud which line they are looking at. ``href``
    points at the mandate's own page holding that row; it is never an external
    URL, because the promise of this page is that a hook can be checked *here*.
    """

    klasse: Klasse
    #: "Marktsignal 1184" — the row, named.
    ref: str
    #: What the row says, in one line.
    label: str
    #: Where the row is shown, inside the application.
    href: str
    #: The source's own public page, when it has one. Rendered in the document,
    #: where an in-app link would be useless to the recipient.
    source_url: str = ""


def _signal_evidence(session: Session, client: Client, hook: PlanHook) -> Evidence | None:
    row = session.get(MarketSignal, hook.source_id)
    if row is None or row.client_id != client.id:
        return None
    publisher = (row.publisher or "").strip()
    return Evidence(
        klasse=_SIGNAL_KLASSEN.get(row.kind, _FALLBACK_KLASSEN[HookSource.MARKTSIGNAL]),
        ref=f"Marktsignal {row.id}",
        label=f"{row.title} ({publisher})" if publisher else row.title,
        # The section of the market page that holds this class of signal. The
        # anchors are the market template's own (``sig-studie`` and its two
        # siblings), so the link lands where the row is rendered rather than at
        # the top of a page the reader then has to search.
        href=f"/client/{client.id}/market#sig-{row.kind.value}",
        source_url=row.url or "",
    )


def _theme_evidence(session: Session, client: Client, hook: PlanHook) -> Evidence | None:
    hit = session.get(TopicHit, hook.source_id)
    if hit is None or hit.client_id != client.id:
        return None
    article = session.get(Article, hit.article_id)
    if article is None:
        return None
    return Evidence(
        klasse=THEMA,
        ref=f"Themen-Treffer {hit.id}",
        label=f"{article.title} ({article.source})",
        # The radar's own section, which is the first thing on the market page.
        href=f"/client/{client.id}/market",
        source_url=article.url or "",
    )


def _archive_evidence(session: Session, client: Client, hook: PlanHook) -> Evidence | None:
    analysis = session.get(Analysis, hook.source_id)
    if analysis is None or analysis.client_id != client.id:
        return None
    article = session.get(Article, analysis.article_id)
    if article is None:
        return None
    month = plan.month_key(article.published_at)
    return Evidence(
        klasse=ARCHIVMUSTER,
        ref=f"Analyse {analysis.id}",
        label=f"{article.title} ({article.source}, {month_name(month)} {month[:4]})",
        # The mandate's own archive, narrowed to the month the evidence lies in:
        # the hook's claim is about that month, so the link has to answer for the
        # month rather than for one headline in it.
        href=f"/client/{client.id}?" + urlencode(_month_filter(month)),
        source_url=article.url or "",
    )


_RESOLVERS = {
    HookSource.MARKTSIGNAL: _signal_evidence,
    HookSource.THEMA: _theme_evidence,
    HookSource.VORJAHR: _archive_evidence,
}


def _month_filter(month: str) -> dict[str, str]:
    """The archive filter that shows exactly one calendar month."""
    year, mon = int(month[:4]), int(month[5:7])
    first = dt.date(year, mon, 1)
    last = (dt.date(year + (mon == 12), (mon % 12) + 1, 1)) - dt.timedelta(days=1)
    return {"date_from": first.isoformat(), "date_to": last.isoformat()}


def evidence_for(session: Session, client: Client, hook: PlanHook) -> Evidence | None:
    """The stored row this hook cites, or ``None`` when it no longer resolves."""
    return _RESOLVERS[hook.source_kind](session, client, hook)


# --- What the page renders per hook ----------------------------------------------


@dataclass(frozen=True, slots=True)
class HookView:
    """One hook, with everything the card shows and nothing it does not."""

    hook: PlanHook
    #: The stored row, or ``None`` when it has since been deleted.
    evidence: Evidence | None
    #: The texts written off this hook, newest first.
    texts: tuple[Asset, ...]
    #: The occasion this hook was opened as, when somebody has opened one.
    angle_id: int | None

    @property
    def klasse(self) -> Klasse:
        return (
            self.evidence.klasse
            if self.evidence is not None
            else _FALLBACK_KLASSEN[self.hook.source_kind]
        )

    @property
    def day_label(self) -> str:
        """The big half of the date block: the day, or the month it stands in.

        A hook with no day says so rather than showing the first of the month.
        The date rule is the whole feature (DEC-4), and rendering an undated
        archive pattern as "01." would put a date on the page that no stored row
        makes.
        """
        if self.hook.day is not None:
            return f"{self.hook.day:02d}"
        return _MONTHS_SHORT[int(self.hook.month[5:7]) - 1]

    @property
    def month_label(self) -> str:
        """The small half: the month, or the honest "no day" beneath it."""
        if self.hook.day is not None:
            return _MONTHS_SHORT[int(self.hook.month[5:7]) - 1]
        return "ohne Tag"

    @property
    def dated(self) -> bool:
        return self.hook.day is not None

    @property
    def state_label(self) -> str:
        return STATE_LABELS[self.hook.state]

    @property
    def accepted(self) -> bool:
        return self.hook.state is HookState.ANGENOMMEN

    @property
    def released(self) -> tuple[Asset, ...]:
        """The texts off this hook that a person has released.

        What makes a hook "erledigt" in the mock. A draft is not done: the whole
        point of the release is that somebody read it and put the agency's name
        on it.
        """
        return tuple(row for row in self.texts if row.released)

    @property
    def done(self) -> bool:
        return bool(self.released)

    @property
    def format_name(self) -> str:
        """The suggested format's German name, or empty when none was suggested."""
        fmt = assets_mod.REGISTRY.get(self.hook.format)
        return fmt.name if fmt is not None else ""


@dataclass(frozen=True, slots=True)
class MonthView:
    """One month of the plan, empty ones included — DEC-5's whole argument."""

    key: str
    name: str
    year: int
    hooks: tuple[HookView, ...]
    #: Whether this is the month the reader is standing in.
    current: bool
    #: Whether this empty month carries the full argument for being empty.
    #:
    #: Only the first one does. The sentence is an argument ("either there
    #: genuinely is nothing here, or the mandate is missing a theme"), and an
    #: argument printed five times down one page stops being read at the second.
    #: Every empty month still says it is empty and still carries the link.
    explain: bool = False

    @property
    def live(self) -> tuple[HookView, ...]:
        """The hooks that still stand: everything a person has not discarded."""
        return tuple(v for v in self.hooks if v.hook.state is not HookState.VERWORFEN)

    @property
    def discarded(self) -> tuple[HookView, ...]:
        return tuple(v for v in self.hooks if v.hook.state is HookState.VERWORFEN)

    @property
    def done(self) -> int:
        return sum(1 for v in self.live if v.done)

    @property
    def empty(self) -> bool:
        return not self.live


def month_name(month: str) -> str:
    """``"2026-09"`` as ``September``."""
    return _MONTHS[int(month[5:7]) - 1]


def _texts_by_hook(session: Session, client: Client) -> dict[int, list[Asset]]:
    """Every text this mandate has written off a plan hook, keyed by hook.

    One query for the page rather than one per hook: a six-month plan carries a
    dozen entries, and a per-hook lookup would be a dozen round trips for a
    column that is empty on most of them.
    """
    rows = session.execute(
        select(Angle.plan_hook_id, Asset)
        .join(Asset, Asset.angle_id == Angle.id)
        .where(Angle.client_id == client.id, Angle.plan_hook_id.is_not(None))
        .order_by(Asset.generated_at.desc(), Asset.id.desc())
    ).all()
    found: dict[int, list[Asset]] = {}
    for hook_id, asset in rows:
        found.setdefault(hook_id, []).append(asset)
    return found


def _angles_by_hook(session: Session, client: Client) -> dict[int, int]:
    """The occasion each hook was opened as, when one has been opened."""
    rows = session.execute(
        select(Angle.plan_hook_id, Angle.id)
        .where(Angle.client_id == client.id, Angle.plan_hook_id.is_not(None))
        .order_by(Angle.id)
    ).all()
    return {hook_id: angle_id for hook_id, angle_id in rows}


def months_for(
    session: Session, client: Client, *, now: dt.datetime | None = None
) -> list[MonthView]:
    """The plan as the page shows it: every window month, empty ones included."""
    reference = now or dt.datetime.now(dt.UTC)
    texts = _texts_by_hook(session, client)
    occasions = _angles_by_hook(session, client)
    current = plan.month_key(reference)
    months = [
        MonthView(
            key=month,
            name=month_name(month),
            year=int(month[:4]),
            current=month == current,
            hooks=tuple(
                HookView(
                    hook=hook,
                    evidence=evidence_for(session, client, hook),
                    texts=tuple(texts.get(hook.id, ())),
                    angle_id=occasions.get(hook.id),
                )
                for hook in hooks
            ),
        )
        for month, hooks in plan.read(session, client, now=reference)
    ]
    first_empty = next((m for m in months if m.empty), None)
    if first_empty is None:
        return months
    return [
        replace(month, explain=True) if month is first_empty else month
        for month in months
    ]


# --- The mandate that cannot have a plan at all -----------------------------------


@dataclass(frozen=True, slots=True)
class Gap:
    """What a mandate is missing before a plan can be built for it at all.

    Rendered instead of the month stack, not beside it. Six empty months read as
    "your market has nothing coming"; this reads as "nobody has told the tool
    what your market is", and only the second one is true.
    """

    themes: bool
    signals: bool


def gap_for(session: Session, client: Client) -> Gap | None:
    """The two missing inputs, or ``None`` when the mandate has either of them.

    Both have to be missing. One of the two is enough for a plan: a mandate with
    themes and no signals still gets theme and archive hooks, and a mandate with
    signals and no themes still gets its dated ones. Only a mandate with neither
    has nothing a hook could be made of, and that is a configuration gap rather
    than a quiet market.
    """
    themes = bool(client.keywords or client.alert_topics)
    signals = bool(
        session.scalar(
            select(MarketSignal.id).where(MarketSignal.client_id == client.id).limit(1)
        )
    )
    if themes or signals:
        return None
    return Gap(themes=themes, signals=signals)


# --- Recompute, on a worker thread ------------------------------------------------
#
# One at a time, process-wide, for the reason every other generate button in this
# app takes a lock: a recompute shells out to a model, and a second click would
# buy the same prose twice.

_recomputing = threading.Lock()

#: What the last click had to say, per mandate. In memory and deliberately not a
#: schema change: it describes one click, not the mandate.
_notes: dict[int, str] = {}

#: The engine seam, as a module attribute rather than a default argument — the
#: same shape ``report._generate`` offers, so a test substitutes it in one place
#: and no route has to thread it through.
_recompute = plan.recompute


def note_for(client_id: int) -> str:
    """What the last recompute click had to say, if anything."""
    return _notes.get(client_id, "")


def busy() -> bool:
    """Whether a plan is being recomputed anywhere in this process."""
    return _recomputing.locked()


def _run_recompute(client_id: int) -> None:
    """Rebuild one mandate's plan on a worker thread; always let go.

    The sweep's guard is taken without waiting, like every other button that
    reaches for it: held blocking, a click during a sweep would sit on a job
    that fetches forty feeds while the page said nothing at all.
    """
    try:
        taken = _run_guard.acquire(blocking=False)
        if not taken:
            _notes[client_id] = _SWEEP_RUNNING
            return
        try:
            with get_session() as session:
                client = session.get(Client, client_id)
                if client is None:
                    return
                _notes.pop(client_id, None)
                _recompute(session, client)
        finally:
            _run_guard.release()
    except Exception as exc:  # noqa: BLE001 — a worker thread must never die silently
        # Never silent: a failed recompute leaves the previous plan standing, and
        # a reader who is not told would read the unchanged page as "nothing has
        # changed in the market".
        _notes[client_id] = _FAILED
        _log.exception("recomputing the plan for client %s failed: %s", client_id, exc)
    finally:
        _recomputing.release()


# --- The occasion a hook is opened as ---------------------------------------------


def occasion_for(session: Session, client: Client, hook: PlanHook) -> Angle:
    """The impulse this hook stands for, created on the first click and reused.

    Reused rather than re-created, and that is what ``Angle.plan_hook_id`` is
    for: a second click on "Text schreiben" must land on the package that is
    already there, not open a second occasion beside it with the same date on it.

    The occasion carries the hook's own words. ``subject`` is what the hook is
    about, ``message`` is the reason the model wrote for it, and ``context`` is
    the evidence — which is exactly what a format prompt reads, so a text written
    off a hook argues from the stored row rather than from a headline.
    """
    found = session.scalars(
        select(Angle).where(
            Angle.client_id == client.id, Angle.plan_hook_id == hook.id
        )
    ).first()
    if found is not None:
        return found
    evidence = evidence_for(session, client, hook)
    angle = Angle(
        client_id=client.id,
        plan_hook_id=hook.id,
        subject=hook.title,
        message=hook.reason or _occasion_fallback(hook),
        context=evidence.label if evidence is not None else "",
        thesis="",
        statements=[],
        article_ids=[],
    )
    session.add(angle)
    session.commit()
    return angle


def _occasion_fallback(hook: PlanHook) -> str:
    """What the occasion says when the model never wrote a reason for the hook.

    A hook exists because of its evidence, so a failed prose call does not stop
    it being stored — and must not stop a text being written off it either. The
    date and the subject are the substance, and both are in hand here.
    """
    when = f"{hook.day:02d}. " if hook.day is not None else ""
    return (
        f"{hook.title} — Termin im Redaktionsplan: "
        f"{when}{month_name(hook.month)} {hook.month[:4]}."
    )


# --- The page ---------------------------------------------------------------------


def _hook_or_404(session: Session, client: Client, hook_id: int) -> PlanHook:
    """The hook, and that it belongs to this mandate.

    Both ids come off the URL, so without the pair check one mandate's page
    could discard another's entry.
    """
    hook = session.get(PlanHook, hook_id)
    if hook is None or hook.client_id != client.id:
        raise HTTPException(status_code=404, detail="Hook not found")
    return hook


def _page_context(session: Session, client: Client, *, now: dt.datetime) -> dict:
    return {
        "client": client,
        "months": months_for(session, client, now=now),
        "gap": gap_for(session, client),
        "klassen": KLASSEN,
        # The format names, so a released text is named the way the consultant
        # says it rather than by its stored key ("gastbeitrag").
        "format_labels": _FORMAT_LABELS,
        "plan_months": config.PLAN_MONTHS,
        "busy": busy(),
        "note": note_for(client.id),
        "last_run": _fetch_last_run(session),
        "header_date": now.astimezone(_local_tz()).date(),
    }


@router.get("/client/{client_id}/plan", response_class=HTMLResponse)
def plan_view(
    request: Request, client_id: int, session: Session = Depends(get_db)
) -> HTMLResponse:
    """The editorial plan: six months, every hook on its evidence."""
    client = mandate_or_404(session, client_id)
    return templates.TemplateResponse(
        request,
        "client_plan.html",
        _page_context(session, client, now=dt.datetime.now(dt.UTC)),
    )


def _back(client_id: int, month: str = "") -> RedirectResponse:
    """Back to the plan, at the month that was acted on."""
    anchor = f"#monat-{month}" if month else ""
    return RedirectResponse(f"/client/{client_id}/plan{anchor}", status_code=_SEE_OTHER)


@router.post("/client/{client_id}/plan/neu")
def recompute_plan(client_id: int, session: Session = Depends(get_db)) -> Response:
    """Rebuild the plan from the evidence as it stands now.

    Only untouched proposals are replaced — that contract is
    :func:`newspulse.plan.recompute`'s and is not restated here. What this route
    owns is the lock and the sentence a refused click leaves behind.
    """
    client = mandate_or_404(session, client_id)
    if not _recomputing.acquire(blocking=False):
        _notes[client.id] = _BUSY
        return _back(client_id)
    _notes.pop(client.id, None)
    spawn.start_or_release(
        _run_recompute,
        args=(client.id,),
        name=f"newspulse-plan-{client.id}",
        release=_recomputing.release,
    )
    return _back(client_id)


@router.post("/client/{client_id}/plan/{hook_id}/verwerfen")
def discard_hook(
    client_id: int, hook_id: int, session: Session = Depends(get_db)
) -> Response:
    """A person refuses the hook. The refusal survives every recompute."""
    client = mandate_or_404(session, client_id)
    hook = _hook_or_404(session, client, hook_id)
    plan.discard(session, hook)
    return _back(client_id, hook.month)


@router.post("/client/{client_id}/plan/{hook_id}/verschieben")
def move_hook(
    client_id: int,
    hook_id: int,
    monat: str = Form(""),
    session: Session = Depends(get_db),
) -> Response:
    """A person moves the hook to another month of the plan.

    A month outside the window is refused with a sentence rather than a 422:
    the value comes off a select on a page that may have been open across
    midnight on the first, and the reader can act on "that month is not in the
    plan" where they cannot act on a validation error.
    """
    client = mandate_or_404(session, client_id)
    hook = _hook_or_404(session, client, hook_id)
    try:
        plan.move(session, hook, monat.strip())
    except ValueError:
        _notes[client.id] = _NOT_A_PLAN_MONTH
    return _back(client_id, hook.month)


@router.post("/client/{client_id}/plan/{hook_id}/text")
def write_from_hook(
    client_id: int,
    hook_id: int,
    session: Session = Depends(get_db),
) -> Response:
    """Open the format picker with this hook as the occasion.

    Three things happen, in this order, and each of them is load-bearing:

    * the hook is **accepted**, because clicking this is a person taking it up —
      and because an untouched hook is exactly what the next recompute is
      allowed to delete, which would cut the occasion loose from its date;
    * the occasion is created or found, so a second click lands on the package
      that already exists;
    * the reader is sent to the format list with the suggested format ticked.

    Nothing is written here. The picker is where a person says which formats they
    want, and spending a model call on a click that only meant "show me" is the
    thing the tick boxes were introduced to stop.
    """
    client = mandate_or_404(session, client_id)
    hook = _hook_or_404(session, client, hook_id)
    plan.accept(session, hook)
    angle = occasion_for(session, client, hook)
    query = {"eintrag": f"anlass-{angle.id}"}
    if hook.format in assets_mod.REGISTRY:
        query["format"] = hook.format
    return RedirectResponse(
        f"/client/{client_id}/advice?{urlencode(query)}#impulse-{angle.id}",
        status_code=_SEE_OTHER,
    )


# --- The document -----------------------------------------------------------------


def _filename(client: Client, months: list[MonthView]) -> str:
    """What the downloaded plan is called: what it is, whose it is, which span.

    The same shape ``report._filename`` builds, so a mandate's plan sorts beside
    its reports and its Pressespiegel in a download folder.
    """
    safe = (
        re.sub(r"[^A-Za-z0-9_-]+", "_", client.name).strip("_") or _FILENAME_FALLBACK
    )
    span = f"{months[0].key}_{months[-1].key}" if months else "leer"
    return f"redaktionsplan_{safe}_{span}.html"


@router.get("/client/{client_id}/plan.html")
def plan_document(
    request: Request, client_id: int, session: Session = Depends(get_db)
) -> Response:
    """The plan as a document, for the retainer meeting.

    A file with no way back into the application in it: the recipient has no
    account, and a plan whose every second line is a dead in-app link reads as
    broken software rather than as a calendar. What the reader keeps is the
    evidence *named* — table, id and what the row says — plus the source's own
    public page where it has one, which is the check they can actually make.
    """
    client = mandate_or_404(session, client_id)
    now = dt.datetime.now(dt.UTC)
    months = months_for(session, client, now=now)
    return templates.TemplateResponse(
        request,
        "client_plan_document.html",
        {
            "client": client,
            "months": months,
            "klassen": KLASSEN,
            "built_at": now,
        },
        headers={
            "Content-Disposition": (
                f'attachment; filename="{_filename(client, months)}"'
            )
        },
    )


__all__ = [
    "KLASSEN",
    "STATE_LABELS",
    "Evidence",
    "Gap",
    "HookView",
    "Klasse",
    "MonthView",
    "busy",
    "evidence_for",
    "gap_for",
    "month_name",
    "months_for",
    "note_for",
    "occasion_for",
    "router",
]
