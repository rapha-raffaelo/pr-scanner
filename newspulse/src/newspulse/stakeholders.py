"""Die Stakeholder-Karte: who learns of it, in what order, and what they want
to know (RIS-03).

The map hangs on the **mandate**, not on the issue. Who the neighbours of a
site are and which association speaks for the industry does not change with
the occasion, and a map reinvented per incident is half wrong per incident.
What an issue or a crisis gets is a *selection* from the standing map, never
rows of its own.

Four disciplines govern this module, and each has one owner here:

* **Proposed from the profile, edited by a person.** :func:`propose_card`
  reads the stored profile lines and proposes groups; a mandate without
  profile entries gets ``None`` — the sentence about what is missing, never an
  invented map. A proposal only ever *adds*: every standing row, hand-set or
  proposed, is skipped, so a row a person set is overwritten by nothing.
* **Every row says who set it.** ``set_by`` carries the ``"modell"`` token or
  the person's name, the same provenance the profile keeps for every
  researched value. A proposal never writes a contact: a guessed name would be
  called on the one evening it matters, so the named gap is the honest row.
* **A selection carries its reason, or it is not stored.**
  :func:`select_for` asks the model which groups this occasion touches; a
  group without a stored sentence why is dropped, the same rule
  ``issue_signals.reason`` holds. The one-sentence ``info_need`` rests on the
  stored lines the prompt was shown and may come back empty — an omission,
  never an invented Betroffenheit.
* **The order that is kept is the person's.** The proposal writes positions
  under the ``"modell"`` token, which is what renders them as an Empfehlung;
  :func:`reorder` writes a person's order under their name, and from then on
  the stored order hangs on law, contract and relationship — of which the
  tool sees only part.

The two model calls here are injectable, and no test exercises them against a
real backend.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from importlib import resources
from string import Template

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import brain, config, profile, prose
from . import crisis as crisis_mod
from .analyzer import ParseError, invoke_with_fallback, strip_code_fence
from .models import (
    CRISIS_DECLARED_BY_MAX,
    STAKEHOLDER_TEXT_MAX,
    Client,
    Crisis,
    Issue,
    Stakeholder,
    StakeholderLevel,
    StakeholderSelection,
)

_log = logging.getLogger(__name__)

_CARD_PROMPT = "prompts/stakeholder_map.txt"
_SELECT_PROMPT = "prompts/stakeholder_select.txt"

#: What ``set_by`` and ``position_set_by`` hold for the machine's half: the
#: same token ``issues.ATTACHED_BY_MODEL`` uses, because it answers the same
#: question — who put this here — and the pages print it the same way.
PROPOSED_BY_MODEL = "modell"

#: How many of an issue's signals the selection prompt lists, newest first.
#: The prompt exists to show what the occasion is, not the whole register row.
_MAX_MATTER_LINES = 10

#: The ladder the card is read down: the influential groups first, because the
#: map is opened in the hour a call order is being decided.
_LEVEL_ORDER = {level: rank for rank, level in enumerate(StakeholderLevel)}


class GroupProposal(BaseModel):
    """One group as the card proposal names it."""

    model_config = ConfigDict(extra="ignore")

    gruppe: str
    betroffenheit: str = ""
    einfluss: str = StakeholderLevel.MITTEL.value
    kanal: str = ""


class CardProposal(BaseModel):
    """The model's answer to "wer gehört auf diese Karte"."""

    model_config = ConfigDict(extra="ignore")

    gruppen: list[GroupProposal] = []


class SelectedGroup(BaseModel):
    """One group of the map the model holds to be touched by the occasion."""

    model_config = ConfigDict(extra="ignore")

    gruppe: str
    begruendung: str = ""
    informationsbedarf: str = ""


class SelectionProposal(BaseModel):
    """The model's selection, in the order it recommends."""

    model_config = ConfigDict(extra="ignore")

    auswahl: list[SelectedGroup] = []


# --- Small shared helpers ----------------------------------------------------------


def _named(by: str) -> str:
    """A person's name as the columns store it: trimmed, defaulted, truncated.

    The same trade the issue register makes: an eighty-character ceiling may
    cost the tail of a sign-in name, never the click. The default is the
    ``"mensch"`` token — never a name nobody typed.
    """
    return ((by or "").strip() or crisis_mod.DECLARED_BY_DEFAULT)[
        :CRISIS_DECLARED_BY_MAX
    ]


def _norm(name: str) -> str:
    """One group name as compared: case and edge whitespace do not make two."""
    return " ".join((name or "").split()).casefold()


def _parse(raw: str, schema: type[BaseModel]) -> BaseModel:
    """The payload out of the model's answer, or :class:`ParseError`."""
    try:
        payload = json.loads(strip_code_fence(raw))
    except json.JSONDecodeError as exc:
        raise ParseError(f"stakeholder answer was not valid JSON: {exc}") from exc
    try:
        return schema.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 — pydantic raises its own type
        raise ParseError(f"stakeholder answer did not match the schema: {exc}") from exc


def _template(resource: str) -> Template:
    text = resources.files("newspulse").joinpath(resource).read_text("utf-8")
    return Template(brain.compose(text))


# --- The standing card -------------------------------------------------------------


def card(session: Session, client: Client) -> list[Stakeholder]:
    """The mandate's standing map, most influential groups first."""
    rows = session.scalars(
        select(Stakeholder).where(Stakeholder.client_id == client.id)
    ).all()
    return sorted(rows, key=lambda row: (_LEVEL_ORDER[row.einfluss], _norm(row.group_name)))


def save_row(
    session: Session,
    client: Client,
    *,
    group: str,
    betroffenheit: str = "",
    einfluss: StakeholderLevel | str = StakeholderLevel.MITTEL,
    contact: str = "",
    channel: str = "",
    by: str,
    row_id: int | None = None,
    now: dt.datetime | None = None,
) -> Stakeholder | None:
    """A person writes one row of the map, and the row says who.

    ``row_id`` edits the standing row; without one, a new group is added — or,
    where the name already stands, the standing row is updated instead of a
    duplicate being refused at the schema. A rename onto a name another row
    already carries returns ``None`` and writes nothing, whichever way it
    arrived: ``_norm`` is this module's idea of one group, and the page says
    the name is taken rather than filing a second row nothing can select.
    Either way ``set_by`` becomes the person's name (and ``brain_version``
    goes back to NULL): a proposed row a person has touched is the person's
    row, its prose their own, and no later proposal overwrites it.

    An empty group name returns ``None`` and writes nothing. An out-of-set
    Einfluss raises: the form only offers the three levels, so anything else
    was submitted around it, and a silently defaulted level would store a
    value the person did not choose, under their name.
    """
    name = " ".join((group or "").split())
    if not name:
        return None
    level = einfluss if isinstance(einfluss, StakeholderLevel) else StakeholderLevel(einfluss)
    standing = session.scalars(
        select(Stakeholder).where(Stakeholder.client_id == client.id)
    ).all()
    same_name = next((r for r in standing if _norm(r.group_name) == _norm(name)), None)
    row: Stakeholder | None = None
    if row_id is not None:
        row = session.get(Stakeholder, row_id)
        if row is None or row.client_id != client.id:
            return None
        if same_name is not None and same_name.id != row.id:
            # A rename onto a name that already stands — in any casing. The
            # schema's UNIQUE compares the stored spelling, so "anwohner"
            # beside "Anwohner" passes it; ``_norm`` is what this module means
            # by one group, and two rows it calls the same collapse in
            # ``select_for``'s by-name lookup, where the loser becomes
            # permanently unselectable without a word on the page. The standing
            # row is the answer, and merging two rows a person told apart is
            # not this function's decision to make.
            return None
    else:
        row = same_name
    if row is None:
        row = Stakeholder(client_id=client.id, group_name=name)
        session.add(row)
    row.group_name = name
    row.betroffenheit = (betroffenheit or "").strip()
    row.einfluss = level
    row.contact = (contact or "").strip()[:STAKEHOLDER_TEXT_MAX]
    row.channel = (channel or "").strip()[:STAKEHOLDER_TEXT_MAX]
    row.set_by = _named(by)
    row.set_at = now or dt.datetime.now(dt.UTC)
    # The row is the person's from here on, and a stamp on their own prose
    # would claim a model call that never happened. ``set_by`` already says
    # who; the version has to stop saying what.
    row.brain_version = None
    try:
        session.commit()
    except IntegrityError:
        # A rename collided with a group that already stands. The standing row
        # is the answer, and silently merging two rows a person told apart is
        # not this function's decision to make.
        session.rollback()
        return None
    return row


def delete_row(session: Session, client: Client, row_id: int) -> bool:
    """Remove one row of the map. Its selections go with it: a selection of a
    group that no longer exists explains nothing.

    The selections are deleted here as well as by the FK's CASCADE, because
    the cascade only bites where the SQLite pragma is on (``db.make_engine``
    turns it on; a bare engine does not), and an orphaned selection row would
    render a group nobody can look up.
    """
    row = session.get(Stakeholder, row_id)
    if row is None or row.client_id != client.id:
        return False
    for selection in session.scalars(
        select(StakeholderSelection).where(
            StakeholderSelection.stakeholder_id == row.id
        )
    ).all():
        session.delete(selection)
    session.delete(row)
    session.commit()
    return True


def _proposed_row(
    group: GroupProposal,
    client: Client,
    *,
    taken: set[str],
    reference: dt.datetime,
    written_under: int | None,
) -> Stakeholder | None:
    """One proposed group as it is filed, or ``None`` where it is dropped.

    Three rules decide, and each is a drop rather than a guess: a nameless
    group, a group the map already holds (hand-set or proposed — a proposal
    only ever adds), and a level outside the closed set. The row carries no
    contact whatever the model volunteered: a guessed name would be called on
    the one evening it matters.
    """
    name = " ".join(group.gruppe.split())
    if not name or _norm(name) in taken:
        return None
    try:
        level = StakeholderLevel(group.einfluss.strip().lower())
    except ValueError:
        _log.warning(
            "stakeholder proposal for %r named an unknown level %r; "
            "the group %r is dropped rather than filed under a guess",
            client.name,
            group.einfluss,
            name,
        )
        return None
    return Stakeholder(
        client_id=client.id,
        group_name=name,
        betroffenheit=prose.plain(group.betroffenheit.strip()),
        einfluss=level,
        contact="",
        channel=prose.plain(group.kanal.strip())[:STAKEHOLDER_TEXT_MAX],
        set_by=PROPOSED_BY_MODEL,
        set_at=reference,
        brain_version=brain.stamp(written_under, what="a stakeholder proposal"),
    )


def propose_card(
    session: Session,
    client: Client,
    *,
    invoke=None,
    now: dt.datetime | None = None,
) -> list[Stakeholder] | None:
    """Propose groups from the profile. ``None`` when there is no profile.

    The two answers are different statements and both are load-bearing:

    * ``None`` — the mandate has no profile entries, so there is nothing to
      propose *from*. The page says what is missing, with the link to where it
      is filled in, and no map is invented.
    * a list (possibly empty) — the profile was read; every returned row was
      added under the ``"modell"`` token, with **no contact**: a guessed name
      would be called on the one evening it matters, so names are a person's
      to add.

    A proposal only adds. Every standing row — hand-set or proposed — is
    skipped by name, so nothing a person set (and nothing a person has merely
    left standing) is overwritten.
    """
    facts = profile.stored(session, client.id)
    lines = profile.as_prompt_lines(facts)
    if not lines:
        return None
    standing = card(session, client)
    taken = {_norm(row.group_name) for row in standing}
    existing = "\n".join(f"- {row.group_name}" for row in standing) or "Noch keine."
    # Captured when the prompt is composed, not when the rows are saved: an
    # edit landing while the model writes changes the next proposal, not this
    # one — the same terms as Angle.brain_version.
    written_under = brain.version(session)
    prompt = _template(_CARD_PROMPT).substitute(
        client_name=client.name,
        industry=(client.industry or "unbekannt"),
        profile=lines,
        existing=existing,
    )
    resolved_invoke = invoke if invoke is not None else invoke_with_fallback
    proposal = _parse(
        resolved_invoke(prompt, timeout=config.ANALYZER_TIMEOUT), CardProposal
    )
    reference = now or dt.datetime.now(dt.UTC)
    added: list[Stakeholder] = []
    for group in proposal.gruppen:
        row = _proposed_row(
            group,
            client,
            taken=taken,
            reference=reference,
            written_under=written_under,
        )
        if row is None:
            continue
        session.add(row)
        taken.add(_norm(row.group_name))
        added.append(row)
    session.commit()
    _log.info(
        "stakeholder proposal for %r added %d group(s)", client.name, len(added)
    )
    return added


# --- The selection at an issue or a crisis -----------------------------------------


def _anchor(
    issue: Issue | None, crisis: Crisis | None
) -> tuple[Issue | None, Crisis | None]:
    """Exactly one occasion, or a :class:`ValueError` that says so."""
    if (issue is None) == (crisis is None):
        raise ValueError(
            "Eine Auswahl hängt an genau einem Issue oder genau einer Krise."
        )
    return issue, crisis


def selection_for(
    session: Session, *, issue: Issue | None = None, crisis: Crisis | None = None
) -> list[StakeholderSelection]:
    """The occasion's stored selection, in the order that stands."""
    issue, crisis = _anchor(issue, crisis)
    where = (
        StakeholderSelection.issue_id == issue.id
        if issue is not None
        else StakeholderSelection.crisis_id == crisis.id
    )
    return list(
        session.scalars(
            select(StakeholderSelection)
            .where(where)
            .order_by(StakeholderSelection.position)
        ).all()
    )


def order_is_recommendation(rows: list[StakeholderSelection]) -> bool:
    """Whether the stored order is still the machine's Empfehlung.

    One human resort renames every row, so any row still carrying the token
    means no person has sorted this list — which is what the page's
    "Empfehlung" marker states.
    """
    return any(row.position_set_by == PROPOSED_BY_MODEL for row in rows)


def _matter_lines(issue: Issue | None, crisis: Crisis | None) -> tuple[str, str]:
    """The occasion as the prompt shows it: a title line, and the signal lines."""
    if issue is not None:
        rows = sorted(issue.signals, key=lambda row: row.happened_at, reverse=True)
        lines = []
        for row in rows[:_MAX_MATTER_LINES]:
            if row.article is not None:
                lines.append(
                    f"- {row.happened_at:%d.%m.%Y}: {row.article.title} "
                    f"({row.article.source})"
                )
            elif row.market_signal is not None:
                lines.append(
                    f"- {row.happened_at:%d.%m.%Y}: Marktsignal: "
                    f"{row.market_signal.title}"
                )
        title = issue.title
        if (issue.description or "").strip():
            title += f" — {issue.description.strip()}"
        return title, "\n".join(lines) or "Keine Signale gespeichert."
    line = (
        f"- {crisis.declared_at:%d.%m.%Y}: {crisis.article.title} "
        f"({crisis.article.source})"
    )
    return f"Erklärte Krise: {crisis.article.title}", line


def _card_lines(rows: list[Stakeholder]) -> str:
    """The standing map as the prompt shows it — stored lines and nothing else."""
    lines = []
    for row in rows:
        detail = row.betroffenheit.strip() or "keine Angabe"
        lines.append(
            f"- {row.group_name}: Betroffenheit laut Karte: {detail}; "
            f"Einfluss: {row.einfluss.value}"
        )
    return "\n".join(lines)


def _selection_row(
    chosen: SelectedGroup,
    by_name: dict[str, Stakeholder],
    *,
    issue: Issue | None,
    crisis: Crisis | None,
    position: int,
    reference: dt.datetime,
    written_under: int | None,
) -> StakeholderSelection | None:
    """One selected group as it is stored, or ``None`` where it is dropped.

    Two rules decide: a group the standing map does not hold (the selection is
    *from* the card, never an invention beside it), and a group without a
    reason (the same price of admission ``issue_signals.reason`` charges).
    ``info_need`` is kept as it came, empty allowed — where the stored lines
    support no sentence, no sentence is the honest row.
    """
    target = by_name.get(_norm(chosen.gruppe))
    if target is None:
        _log.warning(
            "the selection named %r, which the standing map does not hold; "
            "a selection is from the card, so it is dropped",
            chosen.gruppe,
        )
        return None
    reason = prose.plain(chosen.begruendung.strip())
    if not reason:
        _log.warning(
            "the selection offered %r without a reason; a group nobody "
            "can say is affected is not stored",
            target.group_name,
        )
        return None
    return StakeholderSelection(
        issue_id=issue.id if issue is not None else None,
        crisis_id=crisis.id if crisis is not None else None,
        stakeholder_id=target.id,
        reason=reason,
        info_need=prose.plain(chosen.informationsbedarf.strip()),
        position=position,
        position_set_by=PROPOSED_BY_MODEL,
        created_at=reference,
        brain_version=brain.stamp(written_under, what="a stakeholder selection"),
    )


def select_for(
    session: Session,
    *,
    issue: Issue | None = None,
    crisis: Crisis | None = None,
    invoke=None,
    now: dt.datetime | None = None,
) -> list[StakeholderSelection]:
    """Build the occasion's selection from the standing map, reasons included.

    Idempotent: an occasion that already carries a selection hands it back
    unchanged — re-asking would clobber the order a person may have set, which
    is the one stored thing here the tool must not touch.

    The mechanics hand the model only stored lines — the map's own rows and
    the occasion's signals — and three rules decide what is stored back:

    * a group the map does not hold is dropped: the selection is *from* the
      standing card, never an invention beside it;
    * a group without a reason is dropped, and said in the log — the same
      rule the issue register holds for an unjustifiable assignment;
    * ``info_need`` is kept as it came (dash-flattened), empty allowed: where
      the stored lines support no sentence, no sentence is the honest row.

    Positions are written 1..n in the model's recommended order, under the
    ``"modell"`` token — an Empfehlung until :func:`reorder` writes a person's.
    An empty map selects nothing: there is nothing to select from.
    """
    issue, crisis = _anchor(issue, crisis)
    standing = selection_for(session, issue=issue, crisis=crisis)
    if standing:
        return standing
    client_id = issue.client_id if issue is not None else crisis.client_id
    client = session.get(Client, client_id)
    rows = card(session, client)
    if not rows:
        return []
    title, matter = _matter_lines(issue, crisis)
    # Captured with the prompt, the same terms as the proposal's stamp above.
    written_under = brain.version(session)
    prompt = _template(_SELECT_PROMPT).substitute(
        client_name=client.name,
        matter_title=title,
        matter=matter,
        map=_card_lines(rows),
    )
    resolved_invoke = invoke if invoke is not None else invoke_with_fallback
    proposal = _parse(
        resolved_invoke(prompt, timeout=config.ANALYZER_TIMEOUT), SelectionProposal
    )
    by_name = {_norm(row.group_name): row for row in rows}
    reference = now or dt.datetime.now(dt.UTC)
    stored: list[StakeholderSelection] = []
    seen: set[int] = set()
    for chosen in proposal.auswahl:
        row = _selection_row(
            chosen,
            by_name,
            issue=issue,
            crisis=crisis,
            # 1..n in the model's recommended order, counted over what is
            # actually kept: a dropped group leaves no gap in the call order.
            position=len(stored) + 1,
            reference=reference,
            written_under=written_under,
        )
        if row is None or row.stakeholder_id in seen:
            continue
        session.add(row)
        seen.add(row.stakeholder_id)
        stored.append(row)
    session.commit()
    return stored


def reorder(
    session: Session,
    *,
    issue: Issue | None = None,
    crisis: Crisis | None = None,
    ordered_ids: list[int],
    by: str,
) -> list[StakeholderSelection]:
    """A person sorts the selection, and the person's order is what is kept.

    ``ordered_ids`` must name exactly the occasion's rows — no more, no fewer:
    a partial order is not an order, and an id from another occasion must not
    reach across. Every row's ``position_set_by`` becomes the person's name,
    which is what ends the "Empfehlung" marker: from here on the stored order
    hangs on law, contract and relationship, and the tool keeps it.
    """
    rows = selection_for(session, issue=issue, crisis=crisis)
    if [*sorted(row.id for row in rows)] != sorted(ordered_ids):
        raise ValueError("Die Reihenfolge nennt nicht genau die Zeilen der Auswahl.")
    person = _named(by)
    rank = {row_id: position for position, row_id in enumerate(ordered_ids, start=1)}
    for row in rows:
        row.position = rank[row.id]
        row.position_set_by = person
    session.commit()
    return sorted(rows, key=lambda row: row.position)


__all__ = [
    "PROPOSED_BY_MODEL",
    "CardProposal",
    "GroupProposal",
    "SelectedGroup",
    "SelectionProposal",
    "card",
    "delete_row",
    "order_is_recommendation",
    "propose_card",
    "reorder",
    "save_row",
    "select_for",
    "selection_for",
]
