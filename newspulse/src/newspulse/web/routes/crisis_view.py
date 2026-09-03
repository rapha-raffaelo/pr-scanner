"""The crisis page, and the buttons DEC-1 locked.

DEC-1 chose option A: the tool proposes, a person declares. Everything else in
:mod:`newspulse.crisis` is arithmetic over stored rows and could have run itself;
the declare/dismiss/close routes here are the part that may not. Above the
threshold Heute and the Mandantenkarte show an offer and nothing changes — no
tighter cadence, no text, no extra notification — until somebody presses
``Krise erklären`` here. A false alarm then costs one click — ``Verwerfen`` —
rather than a morning in emergency mode, which is the whole reason the decision
was locked that way.

The page itself is DEC-2 option C: two columns. Left, what runs about us,
grouped by story; right, what we have set against it; between them a box naming
what nothing of ours answers yet. The crisis is exactly that distance, and the
page shows it rather than making somebody compute it. The Zeitleiste sits one
click away and becomes the after-action record when the crisis closes.

What is missing is shown as missing. A profile without a crisis contact is the
most important line on this page, not an empty one, and it links to the kickoff
where it gets filled.

The action endpoints are POST-and-redirect for the same reason the triage
buttons are: they are one-click actions inside a list that may have been open in
another tab since before a sweep, and a stale id must cost nothing rather than
the page.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import threading
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import assets as assets_mod
from ... import config, crisis, profile
from ...db import get_session
from ...models import (
    Analysis,
    Article,
    Asset,
    Client,
    Crisis,
    Issue,
    Outreach,
    OutreachReply,
    OutreachState,
    Tonality,
    visible_coverage,
)
from ...outlets import normalize_outlet
from ...stories import cluster
from .. import spawn
from ..app import get_db, templates
from ..mandates import mandate_or_404
from ..redirects import local_target
from ..runlock import guard as _run_guard
from .today import _fetch_last_run, _local_tz

router = APIRouter()

_log = logging.getLogger(__name__)

_SEE_OTHER = 303


def _who(request: Request) -> str:
    """The signed-in person, or the token that says a person pressed the button.

    ``declared_by`` exists to record that a *human* decided. Where sign-in is not
    configured the tool still knows that much — the route is only reachable from
    a form somebody submitted — so it writes :data:`newspulse.crisis.DECLARED_BY_DEFAULT`
    rather than inventing a name nobody typed.
    """
    return str(request.scope.get("user_email") or crisis.DECLARED_BY_DEFAULT)


@router.post("/crisis/declare")
def declare_crisis(
    request: Request,
    client_id: int = Form(...),
    article_id: int = Form(...),
    redirect_to: str = Form("/"),
    session: Session = Depends(get_db),
) -> Response:
    """Turn the offer on Heute into a declared crisis.

    A missing mandate or article is a no-op: the offer is rendered from rows that
    a dismissal or a re-match can remove while the page sits open, and losing the
    dashboard over that would be worse than doing nothing.

    So is a mandate that is not on the roster the offer came from — a competitor,
    or one deactivated while the page sat open. Heute only ever offers for active
    non-competitor mandates, and a crisis declared past that filter would be a
    trap: the cadence would fetch and analyse it every hour while no page renders
    it and no button can close it.

    A second submission is not a second crisis — :func:`newspulse.crisis.declare`
    hands back the standing one — so a double click, a second tab and a reload of
    the POST all land on the same row.
    """
    client = session.get(Client, client_id)
    article = session.get(Article, article_id)
    if (
        client is not None
        and article is not None
        and client.active
        and not client.is_competitor
    ):
        declared = crisis.declare(session, client, article, by=_who(request))
        _log.info(
            "crisis %d declared for %r from the dashboard", declared.id, client.name
        )
    return RedirectResponse(local_target(redirect_to), status_code=_SEE_OTHER)


@router.post("/crisis/{crisis_id}/close")
def close_crisis(
    crisis_id: int,
    reason: str = Form(""),
    redirect_to: str = Form("/"),
    session: Session = Depends(get_db),
) -> Response:
    """Stand a crisis down. The reason is required, and it is required here.

    An empty reason returns to the page without closing anything rather than
    raising: the field is marked ``required`` in the form, so an empty one
    reaching this point means the form was submitted around the browser, and a
    500 is not the answer to that. ``close`` itself still refuses it, so the
    invariant lives in one place. The refusal is not mute, though — the page
    the redirect lands on says why the crisis is still open.
    """
    standing = session.get(Crisis, crisis_id)
    if standing is not None:
        if reason.strip():
            crisis.close(session, standing, reason=reason)
        else:
            _last_note[standing.client_id] = (
                "Die Krise wurde nicht geschlossen: es fehlt die Begründung."
            )
    return RedirectResponse(local_target(redirect_to), status_code=_SEE_OTHER)


@router.post("/crisis/dismiss")
def dismiss_offer(
    request: Request,
    client_id: int = Form(...),
    article_id: int = Form(...),
    redirect_to: str = Form("/"),
    session: Session = Depends(get_db),
) -> Response:
    """The other button on the offer: stand it down without declaring anything.

    Nothing changes except that the same story stops being offered for this
    mandate — on Heute, on the Mandantenkarte and in the notification, which all
    ask :func:`newspulse.crisis.propose`. The guards mirror ``declare``: a stale
    id and a double click both cost nothing, and a mandate off the roster is a
    no-op because it was never offered for.

    One guard is this route's own: the article must have been *analyzed* for
    this mandate. A dismissal pre-silences the article's whole story via
    ``_stood_down``, so an arbitrary (client, article) pair — a mis-aimed or
    forged POST — could suppress a future legitimate proposal before it was
    ever shown. Every offer ``propose`` makes comes out of analyzed rows, so
    the check costs a real click nothing. Deliberately not "must match the
    current offer": the offer's lead can shift to a stronger copy of the same
    story between render and click, and refusing that stale-by-minutes click
    would spend DEC-1's one click twice.
    """
    client = session.get(Client, client_id)
    article = session.get(Article, article_id)
    analyzed = (
        session.execute(
            select(Analysis.id)
            .where(
                Analysis.client_id == client_id, Analysis.article_id == article_id
            )
            .limit(1)
        ).first()
        is not None
    )
    if (
        client is not None
        and article is not None
        and analyzed
        and client.active
        and not client.is_competitor
    ):
        crisis.dismiss(session, client, article, by=_who(request))
    return RedirectResponse(local_target(redirect_to), status_code=_SEE_OTHER)


# --- The page (DEC-2, option C: two columns) ------------------------------------

#: The profile rows the "Wer erreichbar ist" list shows, in the order a crisis
#: needs them. Keys from :data:`newspulse.profile.FIELDS`; the crisis contact
#: first because it is the row this page exists to surface — filled or missing.
_REACHABLE_KEYS: tuple[tuple[str, str], ...] = (
    ("krisenkontakt", "Krisenkontakt"),
    ("sprecher", "Sprecher"),
    ("pressekontakt", "Pressekontakt"),
)

# How a journalist writes a deadline into a mail, read deterministically — no
# model call for a line the consultant can check against the mail one click
# away. Two shapes: a cue word with whatever follows it up to a time or date,
# and a plain "bis <Zeit>". Whatever matched is shown as written, because the
# journalist's own words are the deadline; nothing here computes a timestamp.
_DEADLINE_CUE = re.compile(
    r"(?:frist|deadline|redaktionsschluss)\s*(?:ist|:|-)?\s*"
    r"([^\n.;!?]{0,40}?(?:\d{1,2}(?:[:.]\d{2})?\s*uhr|\d{1,2}:\d{2}"
    r"|\d{1,2}\.\d{1,2}\.(?:\d{2,4})?))",
    re.IGNORECASE,
)
_DEADLINE_BIS = re.compile(
    r"\bbis\s+((?:heute|morgen|montag|dienstag|mittwoch|donnerstag|freitag"
    r"|samstag|sonntag)?\s*(?:\d{1,2}(?:[:.]\d{2})?\s*uhr|\d{1,2}:\d{2}))",
    re.IGNORECASE,
)

#: How much of a request's body the card shows. Enough to know what is being
#: asked, short enough that the mail itself stays the document.
_REQUEST_SNIPPET = 240


def _deadline_from(body: str) -> str:
    """The deadline as the journalist wrote it, or "" when none is stated.

    An empty answer is rendered as "keine Frist genannt" — the honest reading of
    a mail that names none, and better than parsing one out of thin air.
    """
    for pattern in (_DEADLINE_CUE, _DEADLINE_BIS):
        found = pattern.search(body or "")
        if found:
            return " ".join(found.group(1).split())
    return ""


@dataclass(frozen=True, slots=True)
class CoverageRow:
    """One piece of coverage as the left column renders it.

    ``headline``/``source``/``importance`` are :func:`newspulse.stories.cluster`'s
    protocol, the same shape the crisis arithmetic feeds it; the rest rides along
    so the template never touches an ORM object.
    """

    headline: str
    source: str
    importance: int
    article_id: int
    url: str
    published_at: dt.datetime
    tonality: str
    summary: str
    is_trigger: bool


@dataclass(frozen=True, slots=True)
class TextView:
    """One of our texts, as the right column renders it: what it is, and — the
    load-bearing part — where it stands with the checks. Never clean by
    omission: ``state`` comes from :meth:`newspulse.models.Asset.check_state`."""

    kind: str
    name: str
    title: str
    body: str
    speaker: str
    state: str
    released: bool
    released_at: dt.datetime | None
    generated_at: dt.datetime
    edited: bool


@dataclass(frozen=True, slots=True)
class RequestView:
    """One open request from the connected mailbox, with its deadline as the
    journalist wrote it ("" when the mail names none)."""

    sender: str
    outlet: str
    received_at: dt.datetime
    deadline: str
    snippet: str


@dataclass(frozen=True, slots=True)
class ContactRow:
    """One line of "Wer erreichbar ist": a filled value, or a named gap."""

    label: str
    value: str

    @property
    def missing(self) -> bool:
        return not self.value


@dataclass(frozen=True, slots=True)
class Gaps:
    """The box between the columns: what nothing of ours answers yet.

    Each field is a stored fact, not a judgement — a format with no text, a
    request nobody has answered, a profile field nobody has filled. The crisis
    is the distance between the columns, and this names it.
    """

    missing_formats: tuple[str, ...]
    open_requests: int
    next_deadline: str
    missing_contact: bool

    @property
    def any(self) -> bool:
        return bool(self.missing_formats or self.open_requests or self.missing_contact)


@dataclass(frozen=True, slots=True)
class TimelineEvent:
    """One line of the Zeitleiste. ``label`` is a fixed German string the
    template translates; ``detail`` and ``source`` are data and stay as they
    are."""

    at: dt.datetime
    label: str
    detail: str
    source: str = ""


@dataclass(frozen=True, slots=True)
class Duration:
    """How long the crisis has been open (or was), in the pieces the bar prints."""

    days: int
    hours: int
    minutes: int


def _duration(since: dt.datetime, until: dt.datetime) -> Duration:
    total = max(0, int((until - since).total_seconds()) // 60)
    return Duration(days=total // 1440, hours=(total % 1440) // 60, minutes=total % 60)


def _coverage_rows(
    session: Session, client: Client, standing: Crisis
) -> list[CoverageRow]:
    """The crisis's coverage, richest first — the same window the level counts.

    Since one :data:`newspulse.crisis.STORY_WINDOW` before the trigger, so the
    page shows the coverage that started it; up to ``closed_at`` for a closed
    crisis, because its record ends where it ended. Importance-first ordering is
    the clusterer's protocol: the first member of a story becomes its lead.
    """
    trigger = standing.article
    window = [Article.published_at >= trigger.published_at - crisis.STORY_WINDOW]
    if standing.closed_at is not None:
        window.append(Article.published_at <= standing.closed_at)
    pairs = session.execute(
        select(Article, Analysis)
        .join(Analysis, Analysis.article_id == Article.id)
        .where(Analysis.client_id == client.id, visible_coverage(), *window)
        .order_by(Analysis.importance_score.desc(), Article.published_at.asc())
    ).all()
    rows = [
        CoverageRow(
            headline=article.title,
            source=article.source,
            importance=analysis.importance_score,
            article_id=article.id,
            url=article.url,
            published_at=article.published_at,
            tonality=analysis.tonality.value,
            summary=analysis.summary or "",
            is_trigger=article.id == trigger.id,
        )
        for article, analysis in pairs
    ]
    if not any(row.is_trigger for row in rows):
        # The trigger can lack an analysis (declared by hand off dismissed
        # coverage) — the page still owes the article the crisis hangs on.
        rows.insert(
            0,
            CoverageRow(
                headline=trigger.title,
                source=trigger.source,
                importance=0,
                article_id=trigger.id,
                url=trigger.url,
                published_at=trigger.published_at,
                tonality=Tonality.UNBEKANNT.value,
                summary="",
                is_trigger=True,
            ),
        )
    return rows


def _format_name(kind: str) -> str:
    """The German name of a stored format, or the raw kind when the registry no
    longer knows it — shown rather than crashed, because the text still exists."""
    try:
        return assets_mod.definition(kind).name
    except KeyError:
        return kind


def _texts(session: Session, standing: Crisis) -> list[TextView]:
    """Every text hanging on this crisis, newest first, each with its check state."""
    rows = session.scalars(
        select(Asset)
        .where(Asset.crisis_id == standing.id)
        .order_by(Asset.generated_at.desc())
    ).all()
    return [
        TextView(
            kind=row.kind,
            name=_format_name(row.kind),
            title=row.title,
            body=row.body,
            speaker=row.speaker,
            state=row.check_state.value,
            released=row.released,
            released_at=row.released_at,
            generated_at=row.generated_at,
            edited=row.edited_at is not None,
        )
        for row in rows
    ]


def _requests(session: Session, client: Client, standing: Crisis) -> list[RequestView]:
    """The open requests from the connected mailbox, newest first.

    Open means the letter stands in ANTWORT: a journalist wrote back and no
    person has filed the outcome yet. Bounded to the crisis — the same reach the
    coverage column reads, one story window before the trigger, because the
    realistic morning runs trigger → journalist mail → declaration, and a
    request that arrived in the minutes before somebody pressed the button
    belongs to this crisis exactly as much as the coverage does. Up to its close
    for a closed one — this page is the crisis's record, not a second inbox.
    """
    reach = standing.article.published_at - crisis.STORY_WINDOW
    window = [OutreachReply.received_at >= reach]
    if standing.closed_at is not None:
        window.append(OutreachReply.received_at <= standing.closed_at)
    pairs = session.execute(
        select(OutreachReply, Outreach)
        .join(Outreach, Outreach.id == OutreachReply.outreach_id)
        .where(
            Outreach.client_id == client.id,
            Outreach.state == OutreachState.ANTWORT,
            *window,
        )
        .order_by(OutreachReply.received_at.desc())
    ).all()
    return [
        RequestView(
            sender=reply.sender,
            outlet=letter.outlet,
            received_at=reply.received_at,
            deadline=_deadline_from(reply.body),
            snippet=" ".join((reply.body or "").split())[:_REQUEST_SNIPPET],
        )
        for reply, letter in pairs
    ]


def _contacts(session: Session, client: Client) -> list[ContactRow]:
    """The escalation list, from the profile — a missing row stays a row."""
    facts = profile.stored(session, client.id)
    rows = []
    for key, label in _REACHABLE_KEYS:
        fact = facts.get(key)
        rows.append(ContactRow(label=label, value=(fact.value.strip() if fact else "")))
    return rows


def _gaps(
    texts: list[TextView], requests: list[RequestView], contacts: list[ContactRow]
) -> Gaps:
    written = {view.kind for view in texts}
    missing = tuple(
        fmt.name for fmt in assets_mod.CRISIS_FORMATS if fmt.key not in written
    )
    next_deadline = next((r.deadline for r in reversed(requests) if r.deadline), "")
    return Gaps(
        missing_formats=missing,
        open_requests=len(requests),
        next_deadline=next_deadline,
        missing_contact=any(row.missing and row.label == "Krisenkontakt" for row in contacts),
    )


def _timeline(
    standing: Crisis,
    coverage: list[CoverageRow],
    texts: list[TextView],
    requests: list[RequestView],
    prehistory: Issue | None = None,
) -> list[TimelineEvent]:
    """The whole crisis in time order — the after-action record once it closes.

    Every entry is a stored fact with its own timestamp; nothing is summarised.

    ``prehistory`` is the issue this crisis escalated out of (RIS-02), when
    there is one: its opening and its signals are prepended, so the chronology
    begins on the day the first signal arrived rather than on the day of the
    declaration. The signals stay the issue's rows — each line here resolves to
    one of them.
    """
    events = [
        TimelineEvent(
            at=standing.declared_at,
            label="Krise erklärt",
            detail=standing.declared_by,
        )
    ]
    if prehistory is not None:
        events.append(
            TimelineEvent(
                at=prehistory.opened_at,
                label="Issue eröffnet",
                detail=prehistory.title,
            )
        )
        shown = {row.article_id for row in coverage}
        events += [
            TimelineEvent(
                at=row.happened_at,
                label="Signal",
                detail=row.article.title if row.article else row.market_signal.title,
                source=row.article.source if row.article else row.market_signal.publisher,
            )
            for row in prehistory.signals
            # The coverage column already lists what falls inside the crisis's
            # own window; a signal repeated there would count one event twice.
            if row.article_id is None or row.article_id not in shown
        ]
    events += [
        TimelineEvent(
            at=row.published_at,
            label="Beitrag",
            detail=row.headline,
            source=row.source,
        )
        for row in coverage
    ]
    for view in texts:
        events.append(
            TimelineEvent(at=view.generated_at, label="Text entworfen", detail=view.name)
        )
        if view.released and view.released_at is not None:
            events.append(
                TimelineEvent(
                    at=view.released_at, label="Text freigegeben", detail=view.name
                )
            )
    events += [
        TimelineEvent(
            at=view.received_at,
            label="Anfrage",
            detail=view.sender,
            source=view.outlet,
        )
        for view in requests
    ]
    if standing.closed_at is not None:
        events.append(
            TimelineEvent(
                at=standing.closed_at,
                label="Krise geschlossen",
                detail=standing.close_reason,
            )
        )
    return sorted(events, key=lambda event: event.at)


@router.get("/client/{client_id}/krise", response_class=HTMLResponse)
def crisis_page(
    request: Request,
    client_id: int,
    krise: int | None = None,
    zeitleiste: bool = False,
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """The page a crisis morning happens on, and afterwards its record.

    404 for a mandate that never had one — the tab does not exist, so neither
    does the page; a hand-typed URL gets the honest answer. ``?krise=`` selects
    an earlier crisis, the default is the open one or the newest;
    ``?zeitleiste=1`` swaps the two columns for the chronology.
    """
    client = mandate_or_404(session, client_id)
    past = crisis.history(session, client)
    if not past:
        raise HTTPException(status_code=404, detail="No crisis was ever declared")
    selected = next((row for row in past if row.id == krise), None)
    if selected is None:
        selected = next((row for row in past if row.closed_at is None), past[0])

    coverage = _coverage_rows(session, client, selected)
    stories = cluster(coverage)
    # The trigger's story leads the column: it is the story the crisis *is*.
    stories.sort(key=lambda story: not any(m.is_trigger for m in story.members))
    texts = _texts(session, selected)
    requests = _requests(session, client, selected)
    contacts = _contacts(session, client)

    now = dt.datetime.now(dt.UTC)
    return templates.TemplateResponse(
        request,
        "client_crisis.html",
        {
            "header_date": now.astimezone(_local_tz()).date(),
            "last_run": _fetch_last_run(session),
            "client": client,
            "crisis": selected,
            "level_max": crisis.LEVEL_MAX,
            "duration": _duration(selected.declared_at, selected.closed_at or now),
            "sweep_minutes": config.crisis_sweep_minutes(),
            "stories": stories,
            "coverage_count": len(coverage),
            # Normalized like the level's own count, so the header and the bar
            # cannot disagree about how many outlets "9 Medien" is.
            "outlet_count": len(
                {normalize_outlet(row.source) for row in coverage if row.source}
            ),
            "negative_count": sum(
                1 for row in coverage if row.tonality == Tonality.NEGATIV.value
            ),
            "texts": texts,
            "requests": requests,
            "contacts": contacts,
            "gaps": _gaps(texts, requests, contacts),
            "timeline": _timeline(
                selected,
                coverage,
                texts,
                requests,
                prehistory=crisis.prehistory(session, selected),
            )
            if zeitleiste
            else [],
            "zeitleiste": zeitleiste,
            "previous": [row for row in past if row.id != selected.id],
            "busy": _writing.locked() and _writing_for == client.id,
            # Popped, not read: a note describes one click, and showing it once
            # is its whole job. Left in the dict it would outlive its morning —
            # on every later crisis view of this mandate, including old closed
            # ones — until the next run happened to clear it. A run still in
            # progress rewrites it on its next sentence, so consuming a
            # mid-run reload costs nothing.
            "note": _last_note.pop(client.id, ""),
        },
    )


# --- Writing the two crisis texts ------------------------------------------------

# One crisis writer at a time, the same posture as the impulse button: the
# worker shells out to a model and holds the sweep guard while it does.
_writing = threading.Lock()

# Whose run holds the lock. The lock is process-global on purpose — one writer
# at a time — but the *page* is per mandate, and mandate B's button must not
# present mandate A's run as "Wird geschrieben…". Only ever read together with
# ``_writing.locked()``; a stale value under a released lock means nothing.
_writing_for: int | None = None

# Why the last click produced what it produced, per mandate. In memory and not a
# schema change on purpose: it describes one click, and going stale on a restart
# is correct (the texts themselves are on their rows).
_last_note: dict[int, str] = {}


def _run_crisis_texts(client_id: int, crisis_id: int) -> None:
    """Write whichever crisis formats do not exist yet, on a worker thread.

    DEC-3 rides inside :func:`newspulse.assets.produce_crisis`: each draft is
    stored and visible the moment it exists, and the checks run after. A refusal
    (a missing spokesperson, say) is recorded where the page shows it, names the
    field, and leaves the other format's run untouched.
    """
    try:
        with _run_guard:
            with get_session() as session:
                client = session.get(Client, client_id)
                standing = session.get(Crisis, crisis_id)
                if client is None or standing is None:
                    return
                if not crisis.still_open(session, standing):
                    _last_note[client_id] = (
                        "Die Krise wurde geschlossen, bevor der Text entstand. "
                        "Es wurde nichts geschrieben."
                    )
                    return
                notes: list[str] = []

                def _note(sentence: str) -> None:
                    notes.append(sentence)
                    _last_note[client_id] = "\n".join(notes)

                _last_note.pop(client_id, None)
                written = {
                    kind
                    for (kind,) in session.execute(
                        select(Asset.kind).where(Asset.crisis_id == crisis_id)
                    ).all()
                }
                wrote = 0
                for fmt in assets_mod.CRISIS_FORMATS:
                    if fmt.key in written:
                        continue
                    try:
                        assets_mod.produce_crisis(
                            session, fmt, client, standing, note=_note
                        )
                        wrote += 1
                    except assets_mod.RequirementsMissing as exc:
                        # The refusal names the missing field; that sentence is
                        # the page's answer, not a traceback's.
                        _note(str(exc))
                    except Exception as exc:  # noqa: BLE001 — one broken format must not kill the other
                        _log.exception("%s for %r failed", fmt.key, client.name)
                        _note(
                            f"{fmt.name}: Der Entwurf ist mit einem Fehler "
                            f"abgebrochen: {exc}. Details stehen im Log."
                        )
                if not wrote and not notes:
                    _note("Beide Krisenformate liegen bereits vor.")
                _log.info(
                    "crisis texts for %r: %d written", client.name, wrote
                )
    except Exception:  # noqa: BLE001 — a worker thread must never die silently
        _log.exception("crisis text run failed")
        _last_note[client_id] = (
            "Der Lauf ist mit einem Fehler abgebrochen. Details stehen im Log."
        )
    finally:
        _writing.release()


@router.post("/client/{client_id}/krise/text")
def write_crisis_texts(
    client_id: int, session: Session = Depends(get_db)
) -> Response:
    """The page's one button: write what does not exist yet, in the background.

    Refused when a writer is already running — a second click from a stale tab
    must not spend a second model call on the same morning. Refused with a
    note, not silently: this mandate's own run shows its button disabled, but a
    run for a *different* mandate does not, and the click that loses to it has
    to say why nothing happened.
    """
    global _writing_for
    client = mandate_or_404(session, client_id)
    standing = crisis.open_crisis(session, client)
    if standing is not None:
        if _writing.acquire(blocking=False):
            _writing_for = client.id
            spawn.start_or_release(
                _run_crisis_texts,
                args=(client.id, standing.id),
                name=f"newspulse-crisis-texts-{client.id}",
                release=_writing.release,
            )
        elif _writing_for == client.id:
            _last_note[client.id] = "Die Texte werden bereits geschrieben."
        else:
            _last_note[client.id] = (
                "Es schreibt gerade ein Lauf für ein anderes Mandat. "
                "Bitte gleich noch einmal drücken."
            )
    return RedirectResponse(f"/client/{client_id}/krise", status_code=_SEE_OTHER)


# --- What the Mandantenkarte and the tab strip know -------------------------------


@dataclass(frozen=True, slots=True)
class TabState:
    """What one mandate's workspace chrome needs to know about crises.

    ``has_history`` turns the Krise tab on — only ever a *declared* crisis does
    that; a dismissed proposal deliberately does not. ``offer`` is DEC-1's
    question, rendered on the Mandantenkarte with its two buttons.
    """

    has_history: bool = False
    open_id: int | None = None
    offer: crisis.Proposal | None = None


_NO_TAB = TabState()


def crisis_tab(request: Request, client: Client | None) -> TabState:
    """Template entry point, reading the request's session like the sidebar does.

    Empty rather than raising when no session is stashed: a missing tab is a
    better failure than a 500 on every workspace page.
    """
    session = getattr(request.state, "db", None)
    if session is None or client is None or client.is_competitor:
        return _NO_TAB
    standing = crisis.open_crisis(session, client)
    offer = None
    if standing is None and client.active:
        offer = crisis.propose(session, client)
    return TabState(
        has_history=standing is not None or crisis.has_history(session, client),
        open_id=standing.id if standing is not None else None,
        offer=offer,
    )
