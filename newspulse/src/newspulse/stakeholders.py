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
* **The selection stays open to the card it is drawn from.**
  :func:`select_for` is idempotent, or a second click would clobber a person's
  order; on its own that would freeze the list against a map that keeps
  growing. :func:`add_to_selection` is the way back in and only ever appends,
  and :func:`drop_from_selection` takes one group off *this* occasion without
  touching the map every other occasion shares.
* **The order that is kept is the person's.** The proposal writes positions
  under the ``"modell"`` token, which is what renders them as an Empfehlung;
  :func:`reorder` writes a person's order under their name, and from then on
  the stored order hangs on law, contract and relationship — of which the
  tool sees only part. Whenever the row set changes, :func:`_renumber` closes
  the gap 1..n in one place, so no consumer has to tolerate a hole.

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


def _group_name(name: str) -> str:
    """One group name as stored: collapsed, and capped at the column's width.

    The cap is the same ceiling ``contact`` and ``channel`` carry, and it is
    here rather than only on the form because the *model* writes this column
    too: a five-thousand-character ``gruppe`` would be rendered into the map
    header and into every later selection prompt, and the form's ``maxlength``
    guards only the human path.
    """
    return " ".join((name or "").split())[:STAKEHOLDER_TEXT_MAX]


def _rows_at(
    session: Session, *, issue_id: int | None, crisis_id: int | None
) -> list[StakeholderSelection]:
    """One occasion's selection rows by id, in the order that stands.

    By id rather than by ORM object, because :func:`delete_row` renumbers what
    is left of an occasion it holds no :class:`Issue` for — only the foreign
    key the deleted selections carried.
    """
    where = (
        StakeholderSelection.issue_id == issue_id
        if issue_id is not None
        else StakeholderSelection.crisis_id == crisis_id
    )
    return list(
        session.scalars(
            select(StakeholderSelection)
            .where(where)
            .order_by(StakeholderSelection.position)
        ).all()
    )


def _renumber(rows: list[StakeholderSelection]) -> None:
    """Close the gaps in one occasion's order, 1..n, keeping who set it.

    The one place the row set changing is answered, so no consumer has to
    tolerate a hole. ``position_set_by`` is deliberately untouched: closing a
    gap is not a person sorting the list, and rewriting the token here would
    end the "Empfehlung" marker without anybody having decided anything.
    """
    for rank, row in enumerate(
        sorted(rows, key=lambda row: row.position), start=1
    ):
        if row.position != rank:
            row.position = rank


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

    ``row_id`` edits the standing row; without one, a new group is added. A
    submission that names a group the map already holds returns ``None`` and
    writes nothing — whether it arrived as an *add* whose name is taken or as
    a *rename* onto another row's name. ``_norm`` is this module's idea of one
    group, and the page says the name already stands rather than filing a
    second row nothing can select.

    The add form is the reason that refusal is a refusal and not a merge. It
    posts every field, so its blank Ansprechpartner would land on the standing
    row and erase the one value this feature exists never to invent — silently,
    since the person is told nothing about a row that saved. The standing row
    is the answer, and merging two rows a person told apart is not this
    function's decision to make.

    On the edit path ``set_by`` becomes the person's name (and
    ``brain_version`` goes back to NULL): a proposed row a person has touched
    is the person's row, its prose their own, and no later proposal overwrites
    it.

    An empty group name returns ``None`` and writes nothing. An out-of-set
    Einfluss raises: the form only offers the three levels, so anything else
    was submitted around it, and a silently defaulted level would store a
    value the person did not choose, under their name.
    """
    name = _group_name(group)
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
    elif same_name is not None:
        # An *add* whose name the map already holds. Refused rather than
        # merged, and the page carries the sentence saying so: the add form
        # posts a blank Ansprechpartner, so merging would erase the one value
        # of this row that is never invented — without a word to the person,
        # whose click looks exactly like a successful save.
        _log.info(
            "an added group named %r, which the map already holds; the "
            "standing row is kept and nothing is overwritten",
            name,
        )
        return None
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

    Every occasion the removed group stood in is then renumbered 1..n. A
    deletion that left the survivors at ``[2, 3]`` would leave the order form
    prefilled with numbers outside its own range — the page would refuse to
    submit at all, and "Reihenfolge speichern" would be dead for that issue
    until somebody hand-edited the fields.
    """
    row = session.get(Stakeholder, row_id)
    if row is None or row.client_id != client.id:
        return False
    doomed = session.scalars(
        select(StakeholderSelection).where(
            StakeholderSelection.stakeholder_id == row.id
        )
    ).all()
    # Captured before the deletes: afterwards there is no row left to say
    # which occasions have a hole in their order.
    occasions = {(sel.issue_id, sel.crisis_id) for sel in doomed}
    for selection in doomed:
        session.delete(selection)
    session.delete(row)
    session.flush()
    for issue_id, crisis_id in occasions:
        _renumber(_rows_at(session, issue_id=issue_id, crisis_id=crisis_id))
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
    name = _group_name(group.gruppe)
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
    if added:
        # Only where there is something of ours to write. An unconditional
        # commit would flush whatever the caller's session happens to be
        # holding on a proposal that added nothing.
        try:
            session.commit()
        except IntegrityError:
            # A concurrent proposal read the same standing card and filed the
            # same group first. Its rows are on the page and say the same
            # thing; this call adds nothing rather than half of something.
            session.rollback()
            _log.warning(
                "a concurrent stakeholder proposal for %r filed the same "
                "group first; the rows that stand are kept",
                client.name,
            )
            return []
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
    return _rows_at(
        session,
        issue_id=issue.id if issue is not None else None,
        crisis_id=crisis.id if crisis is not None else None,
    )


def order_is_recommendation(rows: list[StakeholderSelection]) -> bool:
    """Whether the stored order is still the machine's Empfehlung.

    One human resort renames every row, so any row still carrying the token
    means no person has sorted this list — which is what the page's
    "Empfehlung" marker states.

    An empty list answers ``False``, which reads as "a person set this order"
    and is only safe because there is no order to mark: every caller draws the
    marker inside its own ``if sel`` guard, and a caller that does not must
    check the list itself first.
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


def _ask_selection(
    session: Session,
    *,
    issue: Issue | None,
    crisis: Crisis | None,
    candidates: list[Stakeholder],
    invoke,
    now: dt.datetime | None,
    start_at: int,
) -> list[StakeholderSelection]:
    """One model call over ``candidates``, and the rows worth storing from it.

    The engine both entry points share. It hands the model only stored lines —
    the candidate rows of the map and the occasion's signals — and three rules
    decide what is stored back:

    * a group the map does not hold is dropped: the selection is *from* the
      standing card, never an invention beside it;
    * a group without a reason is dropped, and said in the log — the same
      rule the issue register holds for an unjustifiable assignment;
    * ``info_need`` is kept as it came (dash-flattened), empty allowed: where
      the stored lines support no sentence, no sentence is the honest row.

    Positions run ``start_at``.. over what is actually kept, so a dropped group
    leaves no gap in the call order.
    """
    client_id = issue.client_id if issue is not None else crisis.client_id
    client = session.get(Client, client_id)
    title, matter = _matter_lines(issue, crisis)
    # Captured with the prompt, the same terms as the proposal's stamp above.
    written_under = brain.version(session)
    prompt = _template(_SELECT_PROMPT).substitute(
        client_name=client.name,
        matter_title=title,
        matter=matter,
        map=_card_lines(candidates),
    )
    resolved_invoke = invoke if invoke is not None else invoke_with_fallback
    proposal = _parse(
        resolved_invoke(prompt, timeout=config.ANALYZER_TIMEOUT), SelectionProposal
    )
    by_name = {_norm(row.group_name): row for row in candidates}
    reference = now or dt.datetime.now(dt.UTC)
    stored: list[StakeholderSelection] = []
    seen: set[int] = set()
    for chosen in proposal.auswahl:
        row = _selection_row(
            chosen,
            by_name,
            issue=issue,
            crisis=crisis,
            position=start_at + len(stored),
            reference=reference,
            written_under=written_under,
        )
        if row is None or row.stakeholder_id in seen:
            continue
        session.add(row)
        seen.add(row.stakeholder_id)
        stored.append(row)
    dropped = len(proposal.auswahl) - len(stored)
    if dropped:
        # Said as a count as well as per row, so a suspiciously thin selection
        # is diagnosable from one line rather than by pairing up warnings.
        _log.info(
            "the selection for %r kept %d of %d offered group(s); %d were "
            "dropped as unknown to the map, unreasoned, or named twice",
            client.name,
            len(stored),
            len(proposal.auswahl),
            dropped,
        )
    try:
        session.commit()
    except IntegrityError:
        # Two clicks landed at once and the other one stored this group first.
        # Its rows are the answer — they say the same thing this call would
        # have — and the caller re-reads what stands.
        session.rollback()
        _log.warning(
            "a concurrent selection for %r stored the same group first; "
            "the rows that stand are kept",
            client.name,
        )
        return []
    return stored


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
    is the one stored thing here the tool must not touch. A map that has grown
    since is reached through :func:`add_to_selection`, which only appends.

    What is stored back, and what is dropped, is :func:`_ask_selection`'s —
    the same three rules both entry points hold.

    Positions are written 1..n in the model's recommended order, under the
    ``"modell"`` token — an Empfehlung until :func:`reorder` writes a person's.
    An empty map selects nothing: there is nothing to select from.
    """
    issue, crisis = _anchor(issue, crisis)
    standing = selection_for(session, issue=issue, crisis=crisis)
    if standing:
        return standing
    client = session.get(
        Client, issue.client_id if issue is not None else crisis.client_id
    )
    rows = card(session, client)
    if not rows:
        return []
    stored = _ask_selection(
        session,
        issue=issue,
        crisis=crisis,
        candidates=rows,
        invoke=invoke,
        now=now,
        start_at=1,
    )
    # Empty where nothing was worth storing, and the other request's rows where
    # a concurrent click won the race: either way, what stands is the answer.
    return stored or selection_for(session, issue=issue, crisis=crisis)


def add_to_selection(
    session: Session,
    *,
    issue: Issue | None = None,
    crisis: Crisis | None = None,
    invoke=None,
    now: dt.datetime | None = None,
) -> list[StakeholderSelection]:
    """Ask whether groups added to the map since also belong here. Appends only.

    :func:`select_for` is idempotent, which is right — re-asking would clobber
    a person's order — but on its own it freezes the selection against the very
    card it is drawn from: a group put on the map on Tuesday could never reach
    Monday's issue. This is the way back in, and it is deliberately narrow:

    * only groups the occasion does not already carry are even offered to the
      model, so a stored reason is never rewritten and no call is spent on a
      question already answered;
    * the new rows land at ``n+1``.., under the ``"modell"`` token, *after*
      whatever stands — a person's order survives an append, which it would not
      survive a re-ask;
    * ``position_set_by`` on the standing rows is left exactly as it is.

    One visible consequence, and it is the right one: the appended row does
    carry the ``"modell"`` token, so the page's pill goes back to reading
    "Empfehlung". The person's own rows keep their names and their numbers —
    but nobody has yet said where the *new* group belongs in the call order,
    and the pill asking them to is more honest than a list that presents a
    machine's placement under their name.

    Returns the rows this call added — empty when the map holds nothing new,
    or when nothing new is reasonably touched by the occasion.
    """
    issue, crisis = _anchor(issue, crisis)
    standing = selection_for(session, issue=issue, crisis=crisis)
    if not standing:
        # An empty selection has nothing to append to; the first question is
        # the whole question, and asking it twice would spend a second call.
        return select_for(
            session, issue=issue, crisis=crisis, invoke=invoke, now=now
        )
    client = session.get(
        Client, issue.client_id if issue is not None else crisis.client_id
    )
    chosen_ids = {row.stakeholder_id for row in standing}
    candidates = [row for row in card(session, client) if row.id not in chosen_ids]
    if not candidates:
        return []
    return _ask_selection(
        session,
        issue=issue,
        crisis=crisis,
        candidates=candidates,
        invoke=invoke,
        now=now,
        start_at=max(row.position for row in standing) + 1,
    )


def drop_from_selection(
    session: Session,
    *,
    issue: Issue | None = None,
    crisis: Crisis | None = None,
    selection_id: int,
) -> bool:
    """A person takes one group off this occasion's list, map untouched.

    The counterpart to :func:`add_to_selection`, and the reason the standing
    map is not the place to do this: removing the row there would take the
    group off *every* occasion and destroy the card. The occasion's remaining
    rows are renumbered 1..n, and ``position_set_by`` is left alone — closing
    a gap is not a person sorting the list.
    """
    issue, crisis = _anchor(issue, crisis)
    rows = selection_for(session, issue=issue, crisis=crisis)
    doomed = next((row for row in rows if row.id == selection_id), None)
    if doomed is None:
        return False
    session.delete(doomed)
    session.flush()
    _renumber([row for row in rows if row.id != selection_id])
    session.commit()
    return True


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
    "add_to_selection",
    "card",
    "delete_row",
    "drop_from_selection",
    "order_is_recommendation",
    "propose_card",
    "reorder",
    "save_row",
    "select_for",
    "selection_for",
]
