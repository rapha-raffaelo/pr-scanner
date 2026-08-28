"""Which mandate profiles have earned a look, and the pass that refreshes them.

The profile is filled once, usually in the week a mandate starts, and then it is a
snapshot with a date on it. A CEO leaves and the letters keep naming them. The
decay is not loud: it is confident, well written, wrong sentences, which is the
worst failure mode a PR tool has.

Two rules shape this module, and both are about restraint.

**It proposes and never writes.** A refresh stores its findings in
``profile_proposals`` and touches ``client_facts`` not at all.
:attr:`~newspulse.models.ClientFact.filled_by` exists precisely because a fact the
consultant knows from a kick-off call and a fact a model read on an about page
must never be confused, and an automatic refresh that silently replaced the first
with the second would destroy the more valuable of the two.

**It is bounded on purpose.** One refresh is a live web search plus a model call.
An unbounded pass over sixty mandates would be both expensive and a good way to
be rate-limited on the day it matters, so a run takes at most
:data:`newspulse.config.PROFILE_REFRESH_PER_RUN` mandates, oldest-due first — a
large portfolio drains over days rather than in one burst.

The due check (DEC-1, option C) is event-driven with an age floor: a mandate whose
own coverage says something moved is looked at now, a quiet one is looked at
eventually. It reads stored state and the clock it is handed, and never the wall
clock — a scheduling rule that cannot be pinned in a test is a scheduling rule
nobody can trust.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from . import config, onboarding, profile
from .clients import list_clients
from .models import (
    Analysis,
    Category,
    Client,
    ClientFact,
    ProfileProposal,
    visible_coverage,
)

_log = logging.getLogger(__name__)

#: How long a profile may go unlooked-at before it becomes due on age alone. The
#: event triggers below spend effort where the news says something happened; this
#: is the floor under them, so a mandate nobody writes about is still checked
#: twice a year rather than never. Sixty days, per DEC-1.
DUE_AFTER = dt.timedelta(days=60)

#: The coverage that suggests a profile moved. A personnel item is what retires
#: the ``ceo`` and ``pressekontakt`` fields; a financial one is what dates
#: ``umsatz``, ``eigentuemer`` and ``mitarbeiter``. An alert counts whatever its
#: category, because an alert is by definition the day something happened.
#: Everything else — a product mention, a routine market piece — is coverage of a
#: company whose profile is still true, and re-researching on it would spend the
#: daily budget on nothing.
_MOVED_CATEGORIES = (Category.PERSONALIE, Category.FINANZEN)

#: One live research at a time in this process, whoever asked for it. The manual
#: button and the 06:10 sweep both land in :func:`refresh`, each with its own
#: session, and each does a DELETE-then-INSERT over one client's proposals.
#: Interleaved, that either loses one side's findings or trips the unique index
#: over the open rows. The web route's own lock only ever kept a second
#: *click* out — the sweep never asked it anything — so the promise has to be
#: made here, where both callers actually meet.
_research_guard = threading.Lock()

#: What :attr:`~newspulse.models.Client.profile_note` says after a check that
#: broke. German, because it is rendered: the note is for the consultant reading
#: the profile page at nine, not for the log the sweep already wrote at 06:10.
_FAILED_NOTE = "Die Recherche ist abgebrochen: {reason}"

#: What the note says when the model answered but cited nothing. A read whose
#: every value is unsourced is filed nowhere (:func:`_sourced`), and without a
#: note that silence would print as "Heute geprüft" with an empty pile: the
#: mandate would look freshly checked, and :func:`_is_due` would keep it out of
#: the sweep for sixty days on the strength of a read that produced nothing.
_UNSOURCED_NOTE = (
    "Die Recherche hat {count} Angabe(n) geliefert, aber keine Quelle dazu. "
    "Nichts davon wurde vorgeschlagen, weil es niemand nachprüfen kann. "
    "Das Profil bleibt zum Abgleich offen."
)

#: The sort position of a mandate that has never been checked. It is the oldest
#: thing there is — a profile filled at kick-off and never looked at since is
#: exactly what this feature exists for — so it sorts ahead of every dated one.
_NEVER_CHECKED = dt.datetime.min.replace(tzinfo=dt.UTC)

# A callable shaped like :func:`newspulse.gemini.search`: a prompt in, the raw
# answer and the pages it was read from out. Injected so a test can drive the
# whole pass over a canned answer with no network anywhere near it, exactly as
# ``FetchFeed`` is injected through the sweep.
Generate = Callable[[str], tuple[str, list[tuple[str, str]]]]


# --- Which profiles have earned a look -----------------------------------------


def _moved_at(session: Session, client_ids: Iterable[int]) -> dict[int, dt.datetime]:
    """When each mandate's own coverage last said something changed.

    Reads the archive the tool already has rather than asking anything: the
    executive change that would date the profile was reported, analysed and filed
    days ago, and the analysis carries both its category and its alert flag.

    One grouped query for the whole portfolio rather than one per mandate. The
    check runs over every client each morning, and sixty mandates asking the same
    question sixty times is sixty round trips for an answer a GROUP BY gives once.
    """
    ids = list(client_ids)
    if not ids:
        return {}
    rows = session.execute(
        select(Analysis.client_id, func.max(Analysis.analyzed_at))
        .where(
            Analysis.client_id.in_(ids),
            visible_coverage(),
            or_(
                Analysis.category.in_(_MOVED_CATEGORIES),
                Analysis.is_alert.is_(True),
            ),
        )
        .group_by(Analysis.client_id)
    ).all()
    return {client_id: latest for client_id, latest in rows if latest is not None}


def _is_due(
    client: Client, *, now: dt.datetime, moved_at: Mapping[int, dt.datetime]
) -> bool:
    """The DEC-1 rule for one mandate: never checked, aged out, or something moved.

    ``moved_at`` is the whole portfolio's answer to "when did this mandate's
    coverage last move", computed once by :func:`_moved_at`. Everything else here
    is stored state on the client and the clock it was handed.
    """
    checked = client.profile_checked_at
    if checked is None:
        return True
    if client.profile_note:
        # The last check broke, so it read nothing. Its stamp records that an
        # attempt happened — which is right, it did — but it must never be read
        # as an answer: used as the watermark below it would bury the personnel
        # item that made this mandate due behind a read that never took place,
        # and one rate-limited morning would quiet a CEO change for sixty days.
        # A mandate whose last read failed stays due until one succeeds.
        return True
    if now - checked >= DUE_AFTER:
        return True
    latest = moved_at.get(client.id or 0)
    return latest is not None and latest > checked


def _oldest_first(client: Client) -> tuple[dt.datetime, int]:
    """Sort key: least recently checked first, ties broken by id so it is stable."""
    return (client.profile_checked_at or _NEVER_CHECKED, client.id or 0)


def due(
    session: Session, clients: Sequence[Client], *, now: dt.datetime
) -> list[Client]:
    """The mandates that have earned a look, oldest-due first.

    A pure function of stored state and the clock it is handed. ``now`` is a value
    rather than a default, and there is deliberately no fallback to
    ``datetime.now`` in here: a bug in this check means either nothing refreshes or
    everything does, and both are only findable if the rule can be driven from a
    frozen clock in a test.

    Competitors are skipped. A competitor is tracked to compare its share of the
    conversation; nobody writes it a pitch, so nothing downstream reads its
    profile and researching one would spend the daily budget on a yardstick.
    """
    watched = [client for client in clients if not client.is_competitor]
    moved_at = _moved_at(session, (client.id for client in watched if client.id))
    candidates = [
        client for client in watched if _is_due(client, now=now, moved_at=moved_at)
    ]
    return sorted(candidates, key=_oldest_first)


# --- The proposal store ---------------------------------------------------------


def may_replace(facts: Mapping[str, ClientFact], key: str) -> bool:
    """Whether a proposal for ``key`` is allowed to be written over what is on file.

    A fact the consultant typed is never overwritten — it may be contradicted,
    visibly, and he decides. That invariant used to live only in the template's
    render filter, which was defensible while the proposal pile was a dict one
    button wrote to: nothing could put a row there that the page had not just
    drawn. It is not defensible now. The unattended sweep files proposals for
    hand-filled fields too (:func:`_as_rows` compares values, not authorship, so
    the contradiction is kept rather than dropped), and a filter that only decides
    what to *draw* is one stale form post away from being walked past.

    So both the review page and the accept route ask this one question. An
    invariant enforced where the page renders is not enforced.
    """
    fact = facts.get(key)
    if fact is None:
        return True
    # Two authorships are the person's, not the machine's: what the consultant
    # typed, and what the client answered in the kick-off. Both are somebody
    # vouching for a value, and the sweep may contradict either, visibly, but
    # overwrite neither.
    return fact.filled_by not in (profile.BY_HAND, onboarding.SOURCE_NAME)


def _on_file(facts: Mapping[str, ClientFact], key: str) -> str:
    """What the profile says for this field right now. Empty when nothing does."""
    fact = facts.get(key)
    return fact.value if fact is not None else ""


def contradicts(facts: Mapping[str, ClientFact], proposal: ProfileProposal) -> bool:
    """Whether this row still says something the profile does not.

    :func:`_as_rows` asks the same question at file time and never stores a row
    that agrees with the profile. That is not enough, because the profile moves
    underneath a row that is already on the pile: the consultant reads "das Netz
    sagt: Paris", types Paris into the field himself, and the row is now a
    contradiction between Paris and Paris.

    Drawing that is merely silly. *Deciding* it is destructive: its Verwerfen
    button would record a refusal against a value the profile itself holds, and
    :func:`_unrefused` would then suppress the field's real correction later — the
    exact state :class:`~newspulse.models.ProfileProposal` says must never exist,
    and the reason an accepted row is deleted rather than stamped. So the question
    is asked again wherever a row is drawn or answered.
    """
    return proposal.value != _on_file(facts, proposal.key)


def outstanding(session: Session, client_id: int) -> list[ProfileProposal]:
    """This client's proposals still waiting for a yes, in the order proposed.

    Refused ones are excluded rather than gone: they stay on file as the record
    of a decision (see :func:`discard`), and nothing outside this module has any
    business seeing them again.
    """
    return list(
        session.scalars(
            select(ProfileProposal)
            .where(
                ProfileProposal.client_id == client_id,
                ProfileProposal.discarded_at.is_(None),
            )
            .order_by(ProfileProposal.id)
        ).all()
    )


def discard(
    session: Session, client_id: int, ids: Sequence[int], *, now: dt.datetime
) -> int:
    """Refuse the named proposals. Stamps them rather than deleting them.

    Identified by row id and not by field, because the two are not the same
    promise. A field name says "whatever is currently proposed for the CEO"; a
    row id says "the thing I was looking at when I clicked". Between the page
    being drawn and the button being pressed the 06:10 sweep may have replaced
    that row with a different value, and sweeping the new one up under the old
    one's name would discard a finding nobody ever saw.

    The stamp is the point. A deleted row leaves no record that anyone decided
    anything, so the next refresh reads the same about page, finds the same
    sentence and offers the same rejected value again — see :func:`_unrefused`.

    Two things are decided here rather than left to the page.

    * **A row the profile has caught up with is dropped, not stamped.** If the
      field now holds the value the row proposes, there is no claim left to
      refuse, and a "no" against a value the profile holds would suppress that
      field's next real correction for good (see :func:`contradicts`). The button
      still does the obvious thing — the row goes away — it simply leaves no
      poison behind.
    * **A refusal is stamped against the fact it argued against.**
      ``previous_value`` is re-read from the profile at this moment rather than
      left at what it was when the row was filed, because that is what the "no"
      is about: not this CEO *while the profile says Anna*. When Anna turns out to
      be wrong and is cleared, the refusal has nothing left to stand on and
      :func:`_unrefused` lets the question be asked again.

    Returns how many rows the form actually acted on, so a caller can tell
    "discarded three" from "there was nothing there".
    """
    wanted = set(ids)
    if not wanted:
        return 0
    facts = profile.stored(session, client_id)
    rows = [row for row in outstanding(session, client_id) if row.id in wanted]
    if not rows:
        return 0
    for row in rows:
        if not contradicts(facts, row):
            _log.info(
                "profile proposal %s (%r) agrees with the profile; dropped rather "
                "than refused, so the field can still be corrected later",
                row.id,
                row.key,
            )
            session.delete(row)
            continue
        row.discarded_at = now
        row.previous_value = _on_file(facts, row.key)
    session.commit()
    return len(rows)


def clear(session: Session, client_id: int, ids: Sequence[int]) -> int:
    """Drop the named proposals outright, for the ones that were accepted.

    Deleted rather than stamped, which is the opposite of :func:`discard` and
    deliberately so: the fact the value became is its own memory, and a refusal
    recorded against a value the profile now holds would suppress a genuine
    correction to it later.

    Open rows only, the same guard :func:`discard` carries. The accept route reads
    its rows and then writes them, and a discard landing in that gap would
    otherwise have its fresh refusal deleted by the accept that overtook it — the
    "no" would vanish with nothing recording that anyone said it.
    """
    wanted = list(ids)
    if not wanted:
        return 0
    removed = (
        session.execute(
            delete(ProfileProposal).where(
                ProfileProposal.client_id == client_id,
                ProfileProposal.id.in_(wanted),
                ProfileProposal.discarded_at.is_(None),
            )
        ).rowcount
        or 0
    )
    session.commit()
    return removed


def _sourced(found: Sequence[profile.Proposal]) -> list[profile.Proposal]:
    """The findings a reader could check, which is the only kind worth filing.

    A proposal nobody can open is a machine asserting something it cannot back
    up; the page does not draw one, so storing it would only put a row on file
    that nothing displays and nobody can clear.

    Separate from :func:`_unrefused` because the two silences mean opposite
    things. "Already answered" is the store working. "No source at all" is a read
    that produced nothing usable, and :func:`refresh` has to say so on the record
    rather than let the mandate go quiet for sixty days looking freshly checked.
    """
    keep: list[profile.Proposal] = []
    for item in found:
        if not item.source_url:
            _log.info(
                "profile refresh: %r came back without a source and was dropped",
                item.key,
            )
            continue
        keep.append(item)
    return keep


def _refused(
    session: Session, client_id: int, keys: Iterable[str]
) -> set[tuple[str, str, str]]:
    """Every (field, value, value-it-argued-against) this client has said no to.

    Values rather than one row per field: the refusal is of a sentence, and a
    field collects as many of them as the web has offered wrong answers for it.
    "Not this CEO" said in March must still be a "no" after a different name was
    refused in April, which is why the schema's uniqueness covers the open rows
    only (see :class:`~newspulse.models.ProfileProposal`).

    The third element is what makes a refusal expire. A "no" is always said
    against something — "not Bob, the CEO is Anna" — and :func:`discard` stamps
    the row with the fact it was refused against. When that fact changes, the
    ground the refusal stood on is gone: clearing a wrong hand-typed Anna has to
    reopen the question, and a refusal with no expiry would instead lock the
    field out of the web permanently.

    Scoped to the keys this read actually covered rather than to the client's
    whole history: refusals accumulate for the life of the mandate and this runs
    on the refresh path.
    """
    wanted = sorted(set(keys))
    if not wanted:
        return set()
    rows = session.execute(
        select(
            ProfileProposal.key,
            ProfileProposal.value,
            ProfileProposal.previous_value,
        ).where(
            ProfileProposal.client_id == client_id,
            ProfileProposal.key.in_(wanted),
            ProfileProposal.discarded_at.is_not(None),
        )
    ).all()
    return {(key, value, against) for key, value, against in rows}


def _unrefused(
    session: Session, client_id: int, found: Sequence[profile.Proposal]
) -> list[profile.Proposal]:
    """The read, minus what this mandate has already answered.

    The web does not change its mind between Tuesday and Wednesday, so an
    unfiltered refresh re-proposes every refused value every time it runs, and a
    pile that keeps asking a question that was answered is a pile that stops
    being opened. A *different* value for the same field is a new claim and does
    go through — the refusal was of a sentence, not of the field — and so is the
    same value once the fact it was refused against has changed.

    Dropped here rather than in :func:`_as_rows` so the key never enters the
    ``covered`` set either: a field the read did cover is *replaced*, and a
    repeated-and-refused value would otherwise take its own refusal with it.
    """
    refused = _refused(session, client_id, (item.key for item in found))
    facts = profile.stored(session, client_id)
    return [
        item
        for item in found
        if (item.key, item.value, _on_file(facts, item.key)) not in refused
    ]


def _as_rows(
    session: Session,
    client: Client,
    found: Sequence[profile.Proposal],
    *,
    now: dt.datetime,
    proposed_by: str,
) -> list[ProfileProposal]:
    """The found values that differ from the profile, as unsaved rows.

    ``found`` is what :func:`_unrefused` let through: sourced, and not something
    this mandate has already answered.

    A proposal identical to what is already on file is noise — the consultant
    would be asked to confirm that nothing changed — so it never becomes a row.
    Each row carries the value it would replace, because "CEO: Alexandre Prot"
    means nothing to a reader who cannot see what it is replacing.
    """
    facts = profile.stored(session, client.id)
    rows: list[ProfileProposal] = []
    for found_value in found:
        fact = facts.get(found_value.key)
        previous = fact.value if fact is not None else ""
        if found_value.value == previous:
            continue
        rows.append(
            ProfileProposal(
                client_id=client.id,
                key=found_value.key,
                value=found_value.value,
                source_url=found_value.source_url,
                source_title=found_value.source_title,
                previous_value=previous,
                proposed_at=now,
                proposed_by=proposed_by,
            )
        )
    return rows


def _unchanged(standing: ProfileProposal, found: ProfileProposal) -> bool:
    """Whether a stored proposal and a fresh one are the same offer, id apart.

    ``previous_value`` counts as part of the offer: a row still proposing Paris
    against a profile that no longer says Berlin is arguing against something
    else now, and it is a different thing to decide.
    """
    return (
        standing.value == found.value
        and standing.source_url == found.source_url
        and standing.source_title == found.source_title
        and standing.previous_value == found.previous_value
    )


def _replace(
    session: Session,
    client_id: int,
    rows: Sequence[ProfileProposal],
    *,
    covered: Iterable[str],
) -> int:
    """Swap this client's open proposals for the fields this read actually
    covered, and say how many rows are genuinely new.

    Replacing rather than adding: a second refresh finding the same three changes
    must leave three proposals, not six. The partial UNIQUE (client_id, key) over
    the open rows is the schema-level backstop for the same promise.

    Scoped to ``covered`` — the keys the answer came back with — rather than the
    client's whole outstanding set, because a grounded search that returns a thin
    ``felder`` object is a *success* that read nothing about the missing fields,
    not a finding that they are settled. Deleting on it would silently erase an
    undecided "CEO changed" nobody had got to yet, which is the one thing a
    proposal store must not do. A field the new read did cover is replaced, even
    when it now agrees with the profile: that proposal has been answered.

    Two rows survive a replace, and each for its own reason.

    * **A refusal.** Only the open rows are swept; a stamped one is this
      mandate's record of a decision and is not the read's to overwrite (see
      :func:`discard`). Sweeping it would put the refused value back on the page
      the next time a website repeated it.
    * **An offer that has not changed.** Same value, same source, same value
      argued against — deleting and re-inserting it would hand it a new id for no
      reason, and the review page's buttons carry ids. The consultant's open tab
      would then click on rows that no longer exist and quietly do nothing.
    """
    keys = sorted(set(covered))
    if not keys:
        return 0
    standing = {
        row.key: row
        for row in session.scalars(
            select(ProfileProposal).where(
                ProfileProposal.client_id == client_id,
                ProfileProposal.key.in_(keys),
                ProfileProposal.discarded_at.is_(None),
            )
        ).all()
    }
    fresh: list[ProfileProposal] = []
    stale: list[int] = []
    for row in rows:
        previous = standing.pop(row.key, None)
        if previous is not None and _unchanged(previous, row):
            continue
        if previous is not None:
            stale.append(previous.id)
        fresh.append(row)
    # Whatever is left in ``standing`` was covered by this read and is not among
    # its findings any more: the field now agrees with the profile, so the
    # question it asked has been answered. Logged, because it is the one place a
    # row nobody decided disappears without anybody clicking anything.
    for row in standing.values():
        _log.debug(
            "profile refresh: open proposal %s (%r) dropped; the field now agrees "
            "with the profile",
            row.id,
            row.key,
        )
    stale.extend(row.id for row in standing.values())
    if stale:
        session.execute(delete(ProfileProposal).where(ProfileProposal.id.in_(stale)))
    session.add_all(fresh)
    return len(fresh)


def _mark_checked(
    session: Session, client: Client, now: dt.datetime, note: str = ""
) -> None:
    """Record that the profile was looked at, whatever the look produced.

    Every attempt, including one that found nothing and one that broke — the same
    posture ``impulse_checked_at`` takes. "Checked, nothing changed" and "never
    checked" are different states, and a page that cannot tell them apart is a
    page that reports a stale profile as fresh.

    ``note`` is why, and it is what keeps the stamp honest. Stamping a failed
    attempt is right — it happened — but a stamp on its own makes a mandate whose
    research died read as "checked today" and quiets its age trigger for sixty
    days with nothing to show for it. So the note is load-bearing in two places
    rather than decorative in one: :func:`_is_due` keeps a mandate due for as long
    as it is set, and the profile page prints it where the check date is read.
    Cleared on a good check, so a stale note cannot outlive the failure it
    describes and cannot keep a healthy mandate permanently due.

    The commit is deliberate and belongs here rather than at the end of the
    sweep: it is the single write that lands this client's new proposals — added
    but not yet flushed by :func:`_replace` — together with the stamp that
    explains them. Splitting the two would allow a crash to keep one without the
    other, and leaving both uncommitted would put the findings back in memory,
    where a restart used to lose them.
    """
    client.profile_checked_at = now
    client.profile_note = note
    session.commit()


# --- One mandate ----------------------------------------------------------------


def adopt(session: Session, client: Client, *, proposed_by: str) -> list[ProfileProposal]:
    """Write in the proposals nobody has to be asked about. Returns what was written.

    "bitte im profil immer alles automatisch mit KI recherchieren und dann
    einpflegen."

    The research already ran every morning; what it produced sat in a pile
    waiting for a click that mostly never came, so the profile the drafting
    prompts read stayed as empty as the day the mandate was created. This closes
    that half.

    :func:`may_replace` decides, and it is the whole safety of this function: a
    field the consultant typed or the client answered in the kick-off is never
    written over. Those proposals stay on the pile as visible contradictions —
    the machine may argue with a person here, and the person decides. Everything
    else is a field that is empty or that a previous read filled, and asking a
    consultant to confirm the machine's correction of the machine is asking him
    to be a clerk.

    ``filled_by`` is the model, never :data:`newspulse.profile.BY_HAND`. Two
    things follow from that and both are the point: the page says where the value
    came from rather than implying somebody vouched for it, and the next read may
    correct it, which a hand-filled value would forbid. Writing these as BY_HAND
    would freeze the first automatic answer forever.

    Only sourced values ever reach the pile (:func:`_sourced`), so nothing
    adopted here is a value the model produced without citing where it read it.
    """
    facts = profile.stored(session, client.id)
    taken = [
        row
        for row in outstanding(session, client.id)
        if may_replace(facts, row.key) and contradicts(facts, row)
    ]
    for row in taken:
        profile.save(
            session,
            client,
            row.key,
            row.value,
            source_url=row.source_url or "",
            source_title=row.source_title or "",
            filled_by=proposed_by,
            # Keeps what the field said beside it where the authors differ — a
            # value this month's model replaced that last month's had written.
            # Without it the change is silent, and the profile feeds every
            # generated text.
            supersede=True,
        )
    clear(session, client.id, [row.id for row in taken])
    if taken:
        _log.info(
            "adopted %d proposal(s) for %r without asking: %s",
            len(taken),
            client.name,
            ", ".join(row.key for row in taken),
        )
    return taken


def refresh(
    session: Session,
    client: Client,
    *,
    now: dt.datetime,
    generate: Generate | None = None,
    proposed_by: str | None = None,
) -> int:
    """Re-research one mandate and store what came back as proposals.

    Writes nothing into ``client_facts``. The whole feature rests on that: an
    automatic pass that overwrote a fact the consultant entered by hand would
    destroy the most valuable data in the tool, and it would do it quietly.

    Serialised process-wide on :data:`_research_guard`: the sweep runs in the same
    process as the dashboard, and two refreshes of the same client racing on its
    proposal rows would lose one side's findings. The wait is bounded by one
    research call and only ever falls on a background worker.

    Returns how many proposals are now outstanding for this client — what the
    review page will show, not what this one read added. Raises whatever the
    research raised: the caller decides whether one failure is worth stopping for
    (:func:`run` decides it is not).
    """
    author = proposed_by or config.review_model()
    with _research_guard:
        try:
            found = profile.research(client, generate=generate)
        except Exception as exc:
            # The attempt happened and belongs on the record even though it
            # failed; the proposals and facts below are untouched, which is what
            # matters. The reason goes on the record with it — the page prints it
            # where the check date is read, and the due check keeps the mandate
            # due until a read actually succeeds.
            _mark_checked(session, client, now, _FAILED_NOTE.format(reason=exc))
            raise
        sourced = _sourced(found)
        fresh = _unrefused(session, client.id, sourced)
        rows = _as_rows(session, client, fresh, now=now, proposed_by=author)
        stored = _replace(session, client.id, rows, covered={item.key for item in fresh})
        # A read that came back with values but cited nothing files nothing, and
        # a stamp with no note would report that as a healthy check: "Heute
        # geprüft", an empty pile, and sixty quiet days before anyone looks
        # again. The note is what keeps the mandate due and tells the reader why.
        note = "" if sourced or not found else _UNSOURCED_NOTE.format(count=len(found))
        if note:
            _log.warning(
                "profile refresh for %r: all %d field(s) came back without a "
                "source; nothing was filed and the profile stays due",
                client.name,
                len(found),
            )
        _mark_checked(session, client, now, note)
        total = len(outstanding(session, client.id))
    _log.info(
        "profile refresh for %r: %d field(s) read, %d already answered or "
        "unsourced, %d new proposal(s), %d outstanding",
        client.name,
        len(found),
        len(found) - len(fresh),
        # What was actually filed. A finding identical to the row already on the
        # page keeps that row and its id, and counting it as new would report
        # movement on a morning when nothing moved.
        stored,
        total,
    )
    return total


# --- The pass over the portfolio ------------------------------------------------


def run(
    session: Session,
    *,
    now: dt.datetime,
    limit: int | None = None,
    generate: Generate | None = None,
) -> int:
    """Refresh the profiles that have earned a look. At most ``limit`` of them.

    ``limit`` defaults to the configured
    :data:`~newspulse.config.PROFILE_REFRESH_PER_RUN`, read here rather than
    frozen into the signature so the operator's ceiling is the one that applies.

    Returns how many mandates were refreshed without error. With nothing due this
    is a no-op that costs one query and writes nothing — and still says so on the
    run line, because "nothing was due" and "the pass never ran" producing the
    same empty output is the confusion the count exists to prevent.

    Fault-isolated per mandate, like every other per-client stage in the sweep: a
    research call that fails is logged at ERROR and the next mandate is tried,
    because a portfolio where one dead website stops the other four from being
    looked at is worse than the one broken profile.
    """
    cap = config.PROFILE_REFRESH_PER_RUN if limit is None else limit
    watched = list_clients(session)
    candidates = due(session, watched, now=now)[:cap]
    refreshed = 0
    adopted = 0
    # Every client, not only the ones due for a read, and deliberately before the
    # reads rather than after each one.
    #
    # Adopting costs no model call and no search — it moves rows that are already
    # on file into the fields they were read for. Gating it on the due check
    # would mean a backlog filed before this existed waits out the sixty-day
    # window before it lands, which is the state the request is about. Measured
    # in production the day this shipped: 76 outstanding proposals, every one of
    # them adoptable, and six of seven mandates with 0 of 18 profile fields
    # filled. The research had been running every night for weeks; none of it had
    # ever reached the profile the drafting prompts read.
    for client in watched:
        try:
            adopted += len(adopt(session, client, proposed_by=config.review_model()))
        except Exception:  # noqa: BLE001 — one bad row must not cost the pass
            session.rollback()
            _log.exception("adopting proposals for %r failed; the pass continues", client.name)
    for client in candidates:
        try:
            refresh(session, client, now=now, generate=generate)
            # Not inside ``refresh``: that function's contract is that it writes
            # nothing into ``client_facts``, and the button on the profile page
            # is built on it — a click reads and proposes, and the consultant
            # decides. This is the unattended pass, which is the one the request
            # is about: "bitte im profil immer alles automatisch mit KI
            # recherchieren und dann einpflegen."
            adopted += len(adopt(session, client, proposed_by=config.review_model()))
        except Exception as exc:  # noqa: BLE001 — per-client fault-isolation boundary
            session.rollback()
            # ``exception`` rather than ``error``: this boundary is wide enough to
            # catch the expected timeout *and* an ordinary bug, and a one-line
            # message with no stack is nothing to debug from. Still ERROR level,
            # which is what the acceptance criterion asks for.
            _log.exception(
                "profile refresh for %r failed: %s; its proposals are unchanged, "
                "the run continues",
                client.name,
                exc,
            )
            continue
        refreshed += 1
    _log.info(
        "profile refresh: %d of %d due mandate(s) refreshed, %d field(s) adopted",
        refreshed,
        len(candidates),
        adopted,
    )
    return refreshed


__all__ = [
    "DUE_AFTER",
    "Generate",
    "adopt",
    "clear",
    "contradicts",
    "discard",
    "due",
    "may_replace",
    "outstanding",
    "refresh",
    "run",
]
