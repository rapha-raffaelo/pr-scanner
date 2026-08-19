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
:data:`REFRESH_PER_RUN` mandates, oldest-due first — a large portfolio drains over
days rather than in one burst.

The due check (DEC-1, option C) is event-driven with an age floor: a mandate whose
own coverage says something moved is looked at now, a quiet one is looked at
eventually. It reads stored state and the clock it is handed, and never the wall
clock — a scheduling rule that cannot be pinned in a test is a scheduling rule
nobody can trust.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Mapping, Sequence

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from . import config, profile
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

#: How many mandates one run may refresh. Each costs a live web search and a model
#: call, so this is a spend ceiling before it is anything else. Five a day clears
#: a sixty-mandate portfolio inside a fortnight without a burst anybody notices.
REFRESH_PER_RUN = 5

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


def _moved_since(session: Session, client_id: int, since: dt.datetime) -> bool:
    """Whether this mandate's own coverage says something changed since ``since``.

    Reads the archive the tool already has rather than asking anything: the
    executive change that would date the profile was reported, analysed and filed
    days ago, and the analysis carries both its category and its alert flag.
    """
    hit = session.scalar(
        select(Analysis.id)
        .where(
            Analysis.client_id == client_id,
            Analysis.analyzed_at > since,
            visible_coverage(),
            or_(
                Analysis.category.in_(_MOVED_CATEGORIES),
                Analysis.is_alert.is_(True),
            ),
        )
        .limit(1)
    )
    return hit is not None


def _is_due(session: Session, client: Client, *, now: dt.datetime) -> bool:
    """The DEC-1 rule for one mandate: never checked, aged out, or something moved."""
    checked = client.profile_checked_at
    if checked is None:
        return True
    if now - checked >= DUE_AFTER:
        return True
    return _moved_since(session, client.id, checked)


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
    candidates = [
        client
        for client in clients
        if not client.is_competitor and _is_due(session, client, now=now)
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
    return fact is None or fact.filled_by != profile.BY_HAND


def outstanding(session: Session, client_id: int) -> list[ProfileProposal]:
    """This client's proposals still waiting for a yes, in the order proposed."""
    return list(
        session.scalars(
            select(ProfileProposal)
            .where(ProfileProposal.client_id == client_id)
            .order_by(ProfileProposal.id)
        ).all()
    )


def discard(
    session: Session, client_id: int, keys: Sequence[str] | None = None
) -> int:
    """Drop this client's outstanding proposals, or only the named fields.

    Returns how many rows went, so a caller can tell "discarded three" from
    "there was nothing there".
    """
    stmt = delete(ProfileProposal).where(ProfileProposal.client_id == client_id)
    if keys is not None:
        stmt = stmt.where(ProfileProposal.key.in_(list(keys)))
    removed = session.execute(stmt).rowcount or 0
    session.commit()
    return removed


def _as_rows(
    session: Session,
    client: Client,
    found: Sequence[profile.Proposal],
    *,
    now: dt.datetime,
    proposed_by: str,
) -> list[ProfileProposal]:
    """The found values that differ from the profile, as unsaved rows.

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


def _replace(
    session: Session, client_id: int, rows: Sequence[ProfileProposal]
) -> None:
    """Swap this client's outstanding proposals for the new set, in one transaction.

    Replacing rather than adding: a second refresh finding the same three changes
    must leave three proposals, not six. The UNIQUE (client_id, key) is the
    schema-level backstop for the same promise.
    """
    session.execute(
        delete(ProfileProposal).where(ProfileProposal.client_id == client_id)
    )
    session.add_all(rows)


def _mark_checked(session: Session, client: Client, now: dt.datetime) -> None:
    """Record that the profile was looked at, whatever the look produced.

    Every attempt, including one that found nothing and one that broke — the same
    posture ``impulse_checked_at`` takes. "Checked, nothing changed" and "never
    checked" are different states, and a page that cannot tell them apart is a
    page that reports a stale profile as fresh.
    """
    client.profile_checked_at = now
    session.commit()


# --- One mandate ----------------------------------------------------------------


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

    Returns how many proposals are now outstanding for this client. Raises
    whatever the research raised — the caller decides whether one failure is worth
    stopping for (:func:`run` decides it is not).
    """
    author = proposed_by or config.review_model()
    try:
        found = profile.research(client, generate=generate)
    except Exception:
        # The attempt happened and belongs on the record even though it failed;
        # the proposals and facts below are untouched, which is what matters.
        _mark_checked(session, client, now)
        raise
    rows = _as_rows(session, client, found, now=now, proposed_by=author)
    _replace(session, client.id, rows)
    _mark_checked(session, client, now)
    _log.info(
        "profile refresh for %r: %d field(s) read, %d proposal(s)",
        client.name,
        len(found),
        len(rows),
    )
    return len(rows)


# --- The pass over the portfolio ------------------------------------------------


def run(
    session: Session,
    *,
    now: dt.datetime,
    limit: int = REFRESH_PER_RUN,
    generate: Generate | None = None,
) -> int:
    """Refresh the profiles that have earned a look. At most ``limit`` of them.

    Returns how many mandates were refreshed without error. With nothing due this
    is a no-op that costs one query and writes nothing.

    Fault-isolated per mandate, like every other per-client stage in the sweep: a
    research call that fails is logged at ERROR and the next mandate is tried,
    because a portfolio where one dead website stops the other four from being
    looked at is worse than the one broken profile.
    """
    candidates = due(session, list_clients(session), now=now)[:limit]
    if not candidates:
        return 0
    refreshed = 0
    for client in candidates:
        try:
            refresh(session, client, now=now, generate=generate)
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
        "profile refresh: %d of %d due mandate(s) refreshed", refreshed, len(candidates)
    )
    return refreshed


__all__ = [
    "DUE_AFTER",
    "REFRESH_PER_RUN",
    "Generate",
    "discard",
    "due",
    "outstanding",
    "refresh",
    "run",
]
