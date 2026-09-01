"""The editorial plan's engine: hooks, and the rule that every one is evidenced.

DEC-4 option A, implemented as a division of labour that mirrors
:mod:`newspulse.report` exactly and for the same reason. The obvious build hands
a model the mandate and asks for six months of occasions; that produces a
calendar nobody checks twice, because the one invented deadline in it reads
exactly as well as the four real ones. So the roles are inverted:

* **The code finds the dates.** Three queries over rows the tool already stores:
  a market signal whose effective date or deadline lies in the future, a theme
  the trade press measurably writes about, and a previous-year month the archive
  shows carried coverage. Each candidate carries the id of the row it came from,
  and a candidate whose row does not resolve is never stored.
* **The model writes prose.** Why this date is an occasion for *this* mandate,
  and which format fits. It is handed the dated candidates and returns reasons
  keyed by reference — never a date, never evidence. A date in its answer has
  nowhere to land: the parse schema has no field for one, and month and day are
  copied from the stored row before the model is ever called.

The date rule follows from the sources. A signal carries a full date, so its
hook carries month *and* day. The archive and a theme's resonance only carry a
month, so their hooks carry a month and no day — the missing day is not guessed,
not defaulted, and not rendered as the first of the month.

Recomputing never throws away a person's work. A hook that was accepted,
discarded or moved to another month survives every recompute; only untouched
proposals inside the window are replaced, and a source a surviving hook already
points at is skipped rather than proposed a second time. A theme is skipped by
its term rather than by its evidence row, because the row that evidences a theme
moves with the radar while the refusal a person recorded does not.

The swap itself is one transaction. A recompute that fails partway gives the
person the plan they had back, not an empty one they will not be offered again
for a week.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from importlib import resources
from string import Template

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import ColumnElement, and_, delete, select
from sqlalchemy.orm import Session

from . import brain, config, prose
from .analyzer import ParseError, invoke_with_fallback, strip_code_fence
from .assets import REGISTRY
from .matching import terms_matcher
from .models import (
    Analysis,
    Article,
    Client,
    HookSource,
    HookState,
    MarketSignal,
    PlanHook,
    Setting,
    TopicHit,
    visible_coverage,
)

_log = logging.getLogger(__name__)

_PROMPT_RESOURCE = "prompts/plan_hooks.txt"

#: How many radar articles must carry a theme's term before the theme counts as
#: measurably resonant. Three, the same floor the radar itself applies
#: (``job._MIN_RADAR_ITEMS``): one hit is noise, and a theme hook off noise is a
#: generic filler with a citation attached — exactly what DEC-4 forbids.
THEME_RESONANCE_MIN = 3

#: How far back that resonance is measured. The same window the theme probe uses
#: (:data:`newspulse.themes.PROBE_DAYS`), so "the press writes about this" means
#: the same thing whether a person measures a proposal or the plan measures a
#: stored theme.
THEME_LOOKBACK = dt.timedelta(days=90)

#: How many pieces of visible coverage a previous-year month needs before it
#: counts as having carried. Three: two articles is an echo, three is a month a
#: consultant would point at in a retainer conversation and say "this is when
#: your subject runs".
VORJAHR_CARRIED_MIN = 3

#: How long a computed plan stands before the sweep recomputes it. Weekly, like
#: the visibility window and for the same reason: the sources move over days,
#: not hours, and a nightly model call per mandate would re-buy the same prose.
PLAN_REFRESH_AFTER = dt.timedelta(days=7)

#: How many candidates one prompt is handed. A plan month holds a handful of
#: hooks; two dozen bounds the call the way ``report._MAX_HEADLINES`` does, and
#: whatever is cut is cut deterministically (signals first, dated before undated).
_MAX_CANDIDATES = 24

#: Key for the last recompute, in the shared settings table — the same shape as
#: ``themes._ATTEMPT_KEY`` and for the same reason: it describes a background
#: job's history, not the mandate, and losing it costs one extra recompute.
_COMPUTED_KEY = "plan_computed_at:{client_id}"


@dataclass(frozen=True, slots=True)
class HookCandidate:
    """One evidenced date, before the model has said anything about it.

    Everything load-bearing is already here: the source row's id, the month, the
    day (or honestly none). ``context`` is the line the prompt shows the model —
    what the row says, so the reason can be about the thing rather than about
    the label.
    """

    kind: HookSource
    source_id: int
    month: str
    day: int | None
    title: str
    context: str


@dataclass(frozen=True, slots=True)
class _Claimed:
    """What this mandate's surviving hooks already speak for.

    Two shapes because a hook has two identities and they are not the same one.
    ``sources`` is the evidence row, which is what ``uq_plan_hooks_source``
    enforces and what a signal or an archive month is fully described by.
    ``themes`` is the term, which is what a theme hook is *about*: its evidence
    row is only the newest article that happened to carry the term, and the
    radar files a newer one most nights. Keying a theme by its evidence would
    let a refusal a person recorded expire with the next article.
    """

    sources: frozenset[tuple[HookSource, int]]
    themes: frozenset[str]


# --- What the model is asked for -------------------------------------------------
#
# Deliberately narrow, and the narrowness is the safety argument: there is no
# field a date could arrive in. ``extra="ignore"`` drops whatever else the model
# volunteers — a "datum", a "beleg", a helpful ISO string — before it can reach
# a row.


class HookProse(BaseModel):
    """The model's two sentences about one candidate, keyed by reference."""

    model_config = ConfigDict(extra="ignore")

    ref: str
    reason: str = ""
    format: str = ""


class HookProseSet(BaseModel):
    """The model's whole answer. An empty list is a legal answer — the hooks
    exist because of their evidence, with or without prose."""

    model_config = ConfigDict(extra="ignore")

    hooks: list[HookProse] = Field(default_factory=list)


# --- The calendar ----------------------------------------------------------------


def month_key(moment: dt.datetime) -> str:
    """``"YYYY-MM"`` for the local calendar month a moment falls in.

    Local, not UTC: the plan is a human calendar, and a regulation effective at
    midnight on the first belongs to the month a Berlin reader files it under,
    not to the UTC evening before.
    """
    local = moment.astimezone(config.local_zone())
    return f"{local.year:04d}-{local.month:02d}"


def month_window(reference: dt.datetime, count: int | None = None) -> list[str]:
    """The plan's months: the current local month and the ones after it."""
    span = config.PLAN_MONTHS if count is None else count
    local = reference.astimezone(config.local_zone())
    year, month = local.year, local.month
    months: list[str] = []
    for _ in range(max(1, span)):
        months.append(f"{year:04d}-{month:02d}")
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return months


def _month_bounds_utc(month: str) -> tuple[dt.datetime, dt.datetime]:
    """UTC ``[start, end)`` of one local calendar month.

    The zone's own offsets on both edges, so the archive query for "September
    last year" means the September a reader lived through rather than a window
    shifted by an hour or two.
    """
    year, mon = int(month[:4]), int(month[5:7])
    zone = config.local_zone()
    start = dt.datetime(year, mon, 1, tzinfo=zone)
    if mon == 12:
        end = dt.datetime(year + 1, 1, 1, tzinfo=zone)
    else:
        end = dt.datetime(year, mon + 1, 1, tzinfo=zone)
    return start.astimezone(dt.UTC), end.astimezone(dt.UTC)


def _previous_year(month: str) -> str:
    return f"{int(month[:4]) - 1:04d}-{month[5:7]}"


# --- The three candidate sources --------------------------------------------------


def _signal_date(signal: MarketSignal, now: dt.datetime) -> dt.datetime | None:
    """The earliest future date a signal carries, or ``None``.

    Both a deadline and an effective date are occasions — "you may still speak"
    and "it now applies to you" each carry a text — and the earlier one is the
    one a plan has to surface first. A signal whose dates all lie in the past is
    news, not a hook.
    """
    future = [
        stamp
        for stamp in (signal.effective_at, signal.deadline_at)
        if stamp is not None and stamp > now
    ]
    return min(future) if future else None


def _signal_candidates(
    session: Session, client: Client, months: list[str], now: dt.datetime
) -> list[HookCandidate]:
    """Every market signal of this mandate with a date inside the window."""
    signals = session.scalars(
        select(MarketSignal)
        .where(MarketSignal.client_id == client.id)
        .order_by(MarketSignal.effective_at, MarketSignal.id)
    ).all()
    candidates: list[HookCandidate] = []
    for signal in signals:
        stamp = _signal_date(signal, now)
        if stamp is None:
            continue
        month = month_key(stamp)
        if month not in months:
            continue
        local = stamp.astimezone(config.local_zone())
        what = "Frist" if stamp == signal.deadline_at else "Termin"
        candidates.append(
            HookCandidate(
                kind=HookSource.MARKTSIGNAL,
                source_id=signal.id,
                month=month,
                day=local.day,
                title=signal.title,
                context=(
                    f"Marktsignal ({signal.kind.value}), {what} am "
                    f"{local.day:02d}.{local.month:02d}.{local.year}: "
                    f"{signal.title} ({signal.publisher or 'ohne Absender'})"
                ),
            )
        )
    return candidates


def _client_themes(client: Client) -> list[str]:
    """The mandate's themes, both lists, deduped in order — the same union the
    radar's own matcher reads (:func:`newspulse.matching.theme_matcher`)."""
    return [
        term.strip()
        for term in dict.fromkeys([*(client.keywords or []), *(client.alert_topics or [])])
        if term and term.strip()
    ]


def _theme_evidence(
    hits: list[tuple[TopicHit, Article]], claimed: _Claimed, spent: set[int]
) -> TopicHit | None:
    """The newest matching radar row no other hook already cites, or ``None``.

    Not simply ``hits[0]``, and the reason is a collision that reaches
    production. Two of a mandate's themes routinely land on one article —
    "Wärmepumpe" and "Heizung" both match a headline about replacing the second
    with the first — while ``topic_hits`` carries exactly one row per (article,
    mandate). The newest matching row would therefore be the *same* row for both
    terms, which is two hooks on one source: ``uq_plan_hooks_source`` refuses the
    second and takes the whole recompute down with it. Each resonant theme cites
    a row of its own instead. A theme whose every hit is already spoken for is
    not stored at all — it waits for the next article, which the radar files
    within days.
    """
    for hit, _ in hits:
        if hit.id in spent or (HookSource.THEMA, hit.id) in claimed.sources:
            continue
        return hit
    return None


def _theme_candidates(
    session: Session,
    client: Client,
    months: list[str],
    now: dt.datetime,
    claimed: _Claimed,
) -> list[HookCandidate]:
    """The themes whose resonance the radar has actually measured.

    Resonance is stored radar material carrying the term, counted over the same
    window the theme probe uses — no live search, no model call. The hook lands
    in the *current* month with no day, because that is the only date the
    evidence supports: "the trade press writes about this now". Spreading a
    theme across the empty months would be exactly the generic filler DEC-4
    forbids.

    A term a surviving hook already carries is skipped here rather than filtered
    out later by its evidence id, because a theme's evidence id is the newest
    matching article and the radar keeps producing newer ones. Filtering by the
    row would re-propose a "verworfen" theme the night after it was refused —
    which is precisely how a person is trained to stop deciding.
    """
    since = now - THEME_LOOKBACK
    rows = session.execute(
        select(TopicHit, Article)
        .join(Article, Article.id == TopicHit.article_id)
        .where(TopicHit.client_id == client.id, Article.published_at >= since)
        .order_by(Article.published_at.desc(), TopicHit.id)
    ).all()
    # Folded once per row, not once per (term, row): a mandate with a dozen
    # themes and a busy quarter of radar would otherwise rebuild and case-fold
    # the same haystack tens of thousands of times for one recompute.
    haystacks = [
        (hit, article, f"{article.title or ''}\n{article.summary_text or ''}".casefold())
        for hit, article in rows
    ]
    candidates: list[HookCandidate] = []
    spent: set[int] = set()
    for term in _client_themes(client):
        if term in claimed.themes:
            continue
        matcher = terms_matcher([term])
        if matcher is None:
            continue
        hits = [
            (hit, article) for hit, article, text in haystacks if matcher.search(text)
        ]
        if len(hits) < THEME_RESONANCE_MIN:
            continue
        evidence = _theme_evidence(hits, claimed, spent)
        if evidence is None:
            continue
        spent.add(evidence.id)
        _, newest = hits[0]
        candidates.append(
            HookCandidate(
                kind=HookSource.THEMA,
                source_id=evidence.id,
                month=months[0],
                day=None,
                title=term,
                context=(
                    f"Thema mit gemessener Resonanz: \"{term}\", {len(hits)} "
                    f"Meldung(en) in {THEME_LOOKBACK.days} Tagen, zuletzt: "
                    f"{newest.title}"
                ),
            )
        )
    return candidates


def _vorjahr_candidates(
    session: Session, client: Client, months: list[str]
) -> list[HookCandidate]:
    """The window's months whose previous-year counterpart carried coverage.

    The only source for the recurring dates that have no fixed date: if the
    mandate's subject ran in September last year, September is worth a slot this
    year. Counted over *visible* coverage — dismissed and irrelevant matches are
    not coverage anywhere else in this tool and cannot carry a plan either. The
    evidence is the month's weightiest analysis (highest importance, then lowest
    id), which is deterministic against an unchanged archive.
    """
    candidates: list[HookCandidate] = []
    for month in months:
        previous = _previous_year(month)
        start, end = _month_bounds_utc(previous)
        rows = session.execute(
            select(Analysis, Article)
            .join(Article, Article.id == Analysis.article_id)
            .where(
                Analysis.client_id == client.id,
                visible_coverage(),
                Article.published_at >= start,
                Article.published_at < end,
            )
            .order_by(Analysis.importance_score.desc(), Analysis.id)
        ).all()
        if len(rows) < VORJAHR_CARRIED_MIN:
            continue
        strongest, headline = rows[0]
        candidates.append(
            HookCandidate(
                kind=HookSource.VORJAHR,
                source_id=strongest.id,
                month=month,
                day=None,
                title=headline.title,
                context=(
                    f"Vorjahresmonat {previous} trug {len(rows)} Beiträge, "
                    f"stärkster: {headline.title}"
                ),
            )
        )
    return candidates


def _candidates(
    session: Session,
    client: Client,
    months: list[str],
    now: dt.datetime,
    claimed: _Claimed,
) -> list[HookCandidate]:
    """Every *fresh* evidenced candidate, dated ones first, capped deterministically.

    Filtered before the cap rather than after it. A mandate in a regulated field
    can carry two dozen future-dated signals a person has long since decided on;
    capping the raw list would spend the whole budget on sources that can never
    be proposed again and truncate the themes and the previous year away before
    either was looked at. The cap exists to bound the prompt, and only what
    survives this filter ever reaches one.

    The dedupe against ``seen`` is the invariant ``uq_plan_hooks_source``
    encodes, enforced where the batch is built. Two candidates on one row is a
    database error at ``add_all`` time, by which point the recompute has already
    decided what the plan should be — cheaper to notice here.
    """
    gathered = [
        *_signal_candidates(session, client, months, now),
        *_theme_candidates(session, client, months, now, claimed),
        *_vorjahr_candidates(session, client, months),
    ]
    fresh: list[HookCandidate] = []
    seen: set[tuple[HookSource, int]] = set()
    for candidate in gathered:
        source = (candidate.kind, candidate.source_id)
        if source in claimed.sources or source in seen:
            continue
        if not _resolves(session, candidate.kind, candidate.source_id):
            continue
        seen.add(source)
        fresh.append(candidate)
    return fresh[:_MAX_CANDIDATES]


# --- Evidence resolution ----------------------------------------------------------

_SOURCE_TABLES = {
    HookSource.MARKTSIGNAL: MarketSignal,
    HookSource.THEMA: TopicHit,
    HookSource.VORJAHR: Analysis,
}


def _resolves(session: Session, kind: HookSource, source_id: int) -> bool:
    """Whether the evidence a hook claims actually exists as a stored row.

    The last gate before a hook is written, and the acceptance rule in one
    function: a hook whose evidence does not resolve is not a weak hook, it is
    not a hook. By construction the candidates come out of queries over these
    very tables, so this catches the drift cases — a row deleted between query
    and store, or a caller handing in an id it made up.
    """
    return session.get(_SOURCE_TABLES[kind], source_id) is not None


# --- The model's prose ------------------------------------------------------------


def _prompt_template() -> Template:
    text = resources.files("newspulse").joinpath(_PROMPT_RESOURCE).read_text("utf-8")
    return Template(brain.compose(text))


def _parse(raw: str) -> HookProseSet:
    try:
        payload = json.loads(strip_code_fence(raw))
    except json.JSONDecodeError as exc:
        raise ParseError(f"hook prose was not valid JSON: {exc}") from exc
    try:
        return HookProseSet.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 — pydantic raises its own type
        raise ParseError(f"hook prose did not match the schema: {exc}") from exc


def _describe(index: int, candidate: HookCandidate) -> str:
    return f"K{index}: {candidate.context}"


def _ask_for_prose(
    client: Client, candidates: list[HookCandidate], invoke
) -> dict[str, HookProse]:
    """One model call for the whole candidate list, keyed ``K1``…``Kn``.

    A failure costs the prose and nothing else: the hooks are stored either way,
    because the date and the evidence are the substance and both are already in
    hand. The empty dict is that honest fallback.
    """
    listing = "\n".join(
        _describe(index, candidate) for index, candidate in enumerate(candidates, 1)
    )
    prompt = _prompt_template().substitute(
        client_name=client.name,
        industry=client.industry or "—",
        candidates=listing,
        formats=", ".join(sorted(REGISTRY)),
    )
    try:
        answer = _parse(invoke(prompt, timeout=config.ANALYZER_TIMEOUT))
    except Exception as exc:  # noqa: BLE001 — prose is not worth losing the dates
        _log.warning("hook prose for %r failed: %s; storing hooks without it", client.name, exc)
        return {}
    return {entry.ref.strip(): entry for entry in answer.hooks}


def _prose_fields(entry: HookProse | None) -> tuple[str, str]:
    """The (reason, format) a hook may carry, cleaned.

    The format has to be a key the registry knows — the plan page pre-selects it
    in the format picker, and an invented key would break that click — and the
    reason goes through the house's dash filter like every other generated text.
    Whatever else the model said about this candidate (a date, most importantly)
    never reaches here: the schema has no field it could have arrived in.
    """
    if entry is None:
        return "", ""
    reason = prose.plain(entry.reason.strip())
    fmt = entry.format.strip()
    if fmt and fmt not in REGISTRY:
        _log.info("dropping unknown format suggestion %r", fmt)
        fmt = ""
    return reason, fmt


# --- Recompute --------------------------------------------------------------------


def _reference(now: dt.datetime | None) -> dt.datetime:
    """The moment a plan is counted from: ``now``, or the clock.

    A naive value is read as UTC rather than rejected, the same reading
    :class:`~newspulse.models.UTCDateTime` gives one on the way into the
    database and the same one :func:`newspulse.visibility._reference` gives.
    Without it a naive ``now`` would silently reinterpret the calendar in
    :func:`month_key` — ``astimezone`` reads a naive value as the *host's* local
    time — and would raise ``TypeError`` in :func:`due`, where the stored stamp
    always comes back aware.
    """
    reference = now or dt.datetime.now(dt.UTC)
    if reference.tzinfo is None:
        return reference.replace(tzinfo=dt.UTC)
    return reference


def _replaceable(months: list[str]) -> ColumnElement[bool]:
    """The hooks a recompute may throw away: proposals inside the window that
    nobody has touched.

    Written once and read twice — by the delete and by :func:`_claimed` — because
    the two must not drift. A row the survivor scan counts but the delete removes
    would leave its source blocked and its hook gone; a row the delete keeps but
    the scan misses would be proposed a second time.
    """
    return and_(
        PlanHook.state == HookState.VORGESCHLAGEN,
        PlanHook.moved_at.is_(None),
        PlanHook.month >= months[0],
    )


def _claimed(session: Session, client: Client, months: list[str]) -> _Claimed:
    """What this mandate's surviving hooks already speak for.

    Read *before* the delete rather than after it, so the model call happens
    outside any open write transaction: the swap in :func:`_replace` is then a
    single short transaction rather than one held open for the length of a
    model call.
    """
    rows = session.scalars(
        select(PlanHook).where(
            PlanHook.client_id == client.id, ~_replaceable(months)
        )
    ).all()
    return _Claimed(
        sources=frozenset((row.source_kind, row.source_id) for row in rows),
        themes=frozenset(
            row.title for row in rows if row.source_kind is HookSource.THEMA
        ),
    )


def _replace(
    session: Session, client: Client, months: list[str], hooks: list[PlanHook]
) -> None:
    """Swap the replaceable proposals for the fresh ones, in one transaction.

    Atomic on purpose, and the failure it bounds is a real one. With the delete
    committed on its own, anything raising between the two commits — a database
    read, an integrity error on the batch — left the mandate with an emptied
    plan *and* a weekly stamp already written, so the next six sweeps would not
    even try again. One transaction means a failed recompute hands the person
    back the plan they had.
    """
    session.execute(
        delete(PlanHook).where(PlanHook.client_id == client.id, _replaceable(months))
    )
    session.add_all(hooks)
    session.commit()


def recompute(
    session: Session,
    client: Client,
    *,
    invoke=invoke_with_fallback,
    now: dt.datetime | None = None,
) -> list[PlanHook]:
    """Rebuild this mandate's plan window; return the hooks newly stored.

    The contract, in order:

    * Only untouched proposals inside the window are deleted. Accepted,
      discarded and moved hooks all survive, and so does everything before the
      window — an old hook falls out of the *read*, never out of the table.
    * A source a surviving hook already points at is not proposed again, and
      neither is a theme one already carries, so a "verworfen" stays refused and
      a moved hook does not reappear in its old month.
    * The model is asked once, about the whole candidate list, and only when
      there is one — a mandate with no evidence costs no call. Its answer
      contributes prose and a format suggestion; month, day and evidence are
      already fixed before the call is made.
    * A candidate whose evidence does not resolve is dropped, not stored.
    * The delete and the insert land together. The plan a person had is only
      gone once its replacement is written in the same transaction.
    """
    reference = _reference(now)
    months = month_window(reference)
    _record_computed(session, client, reference)

    fresh = _candidates(
        session, client, months, reference, _claimed(session, client, months)
    )
    hooks: list[PlanHook] = []
    if fresh:
        # Captured before the model call, the way angles does it: a consultant
        # editing a standard while the sweep runs must not retroactively change
        # what a stored reason claims to have been written under.
        written_under = brain.version(session)
        prose_by_ref = _ask_for_prose(client, fresh, invoke)
        for index, candidate in enumerate(fresh, 1):
            reason, fmt = _prose_fields(prose_by_ref.get(f"K{index}"))
            hooks.append(
                PlanHook(
                    client_id=client.id,
                    source_kind=candidate.kind,
                    source_id=candidate.source_id,
                    month=candidate.month,
                    day=candidate.day,
                    title=candidate.title,
                    reason=reason,
                    format=fmt,
                    brain_version=brain.stamp(written_under, what="a plan hook"),
                )
            )
    _replace(session, client, months, hooks)
    _log.info("plan recomputed for %r: %d hook(s)", client.name, len(hooks))
    return hooks


def due(
    session: Session, client: Client, *, now: dt.datetime | None = None
) -> bool:
    """Whether the sweep should recompute this mandate's plan yet.

    Stamped at the *start* of a recompute (the same posture as
    :func:`newspulse.themes._attempt_is_due`): the failure this bounds is
    "recomputes every night and pays a model call each time", and a run that
    crashes halfway is exactly that.
    """
    row = session.get(Setting, _COMPUTED_KEY.format(client_id=client.id))
    if row is None or not row.value:
        return True
    try:
        last = dt.datetime.fromisoformat(row.value)
    except ValueError:  # hand-edited or from an older format
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=dt.UTC)
    return _reference(now) - last >= PLAN_REFRESH_AFTER


def _record_computed(session: Session, client: Client, reference: dt.datetime) -> None:
    key = _COMPUTED_KEY.format(client_id=client.id)
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=reference.isoformat()))
    else:
        row.value = reference.isoformat()
    session.commit()


# --- Reading and deciding ---------------------------------------------------------


def read(
    session: Session, client: Client, *, now: dt.datetime | None = None
) -> list[tuple[str, list[PlanHook]]]:
    """The plan as the page will show it: every window month, empty ones included.

    An empty month is an answer ("nothing is evidenced here"), so it appears as
    a month with no hooks rather than being skipped — DEC-5's "ein leerer Monat
    ist eine Aussage". Hooks before the window are simply not returned; they
    stay stored, state and all.
    """
    months = month_window(_reference(now))
    rows = session.scalars(
        select(PlanHook).where(
            PlanHook.client_id == client.id, PlanHook.month >= months[0]
        )
    ).all()
    by_month: dict[str, list[PlanHook]] = {month: [] for month in months}
    for hook in rows:
        if hook.month in by_month:
            by_month[hook.month].append(hook)
    for bucket in by_month.values():
        # Dated hooks in date order, undated ones after them: an undated hook
        # rendered above the 5th would read as "before the 5th", which is a
        # claim its source does not make.
        bucket.sort(key=lambda h: (h.day is None, h.day or 0, h.id))
    return [(month, by_month[month]) for month in months]


def accept(
    session: Session, hook: PlanHook, *, now: dt.datetime | None = None
) -> PlanHook:
    """A person takes the hook up. Survives every later recompute."""
    hook.state = HookState.ANGENOMMEN
    hook.decided_at = _reference(now)
    session.commit()
    return hook


def discard(
    session: Session, hook: PlanHook, *, now: dt.datetime | None = None
) -> PlanHook:
    """A person refuses the hook. It stays as a row — the refusal is what stops
    the next recompute from proposing the same source again."""
    hook.state = HookState.VERWORFEN
    hook.decided_at = _reference(now)
    session.commit()
    return hook


def move(
    session: Session, hook: PlanHook, month: str, *, now: dt.datetime | None = None
) -> PlanHook:
    """A person moves the hook to a month the plan actually reaches.

    The move is a touch: the hook survives recomputes from here on, whatever its
    state. The day is cleared — the source's date belongs to the source's month,
    and carrying it into a month a person chose would date the hook to a day
    nobody named.

    The target has to be inside the window, and membership in it is the whole
    validation: every malformed string fails the same check, so there is no
    hand-rolled month parser here to disagree with :func:`month_window`. Beyond
    the window a hook would be stored, would count as touched for good, and
    would never appear in :func:`read` again — thrown away silently, with no
    page left to get it back from. Six months is what DEC-5 renders, so six
    months is what a person can move within.
    """
    window = month_window(_reference(now))
    if month not in window:
        raise ValueError(
            f"not a plan month: {month!r} "
            f"(expected one of {window[0]} … {window[-1]})"
        )
    if month != hook.month:
        hook.day = None
    hook.month = month
    hook.moved_at = _reference(now)
    session.commit()
    return hook


__all__ = [
    "PLAN_REFRESH_AFTER",
    "THEME_LOOKBACK",
    "THEME_RESONANCE_MIN",
    "VORJAHR_CARRIED_MIN",
    "HookCandidate",
    "accept",
    "discard",
    "due",
    "month_key",
    "month_window",
    "move",
    "read",
    "recompute",
]
