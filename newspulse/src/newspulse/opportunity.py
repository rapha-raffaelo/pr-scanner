"""Das Dossier zur Gelegenheit: what the tool knows about one story for one mandate.

Everything a consultant needs about a fast-lane opening is already in the
database — the pieces carrying the story, the standing that was checked, the
occasion that was opened, the texts hanging on it, who could receive them. It
was simply never in one place, and the fast lane runs in hours, so the gathering
was exactly the work there was no time for.

**This module only reads.** Not as thrift, as the load-bearing property. The
existing path from card to text writes an ``Angle`` on the click, and a page
doing the same on *view* would leave an occasion behind for every opportunity
anybody ever looked at. So: no ``session.add``, no commit, no model call, no
network. The dossier shows an occasion when one exists and otherwise the button
that creates one.

What is computed here, and what is refused:

* **The urgency number** (DEC-2) is arithmetic over three stored quantities, and
  :class:`Urgency` carries the three addends rather than only the sum, so the
  page can print the calculation next to the badge and a reader can check it.
* **The next step** (:func:`steps`) is a state machine over stored rows, not a
  judgement. No occasion means write a text; an occasion with no text means pick
  a format; an unchecked text means run the check; a checked text with no letter
  means send. It is the one thing on the page that tells a consultant what to do,
  and it can do that precisely because it never guesses.
* **The forward look** is read, never made. DEC-3 puts it on the row at
  detection (:attr:`newspulse.models.NewsjackOpportunity.outlook`); an empty one
  is rendered as the estimate that was never made, not as an empty calendar.
* **A recipient's fit** (DEC-5) is arithmetic over three stored quantities as
  well, and never a model call: a guessed fit between a person and a story is
  the kind of number nobody checks and everybody believes.

The story's other pieces — every article carrying it, not only the origin the
row anchors — are re-derived by clustering the mandate's stored radar with the
same clusterer :func:`newspulse.newsjack.scan` used when the row was written.
That is a read over stored rows, and it is what lets the sources list name all
four outlets when the row itself knows one. Re-derived, not reproduced: see
:func:`_story_members` for the one way the two reads differ and why the badge
never counts this list.

This module calls a handful of private helpers in :mod:`newspulse.newsjack` and
:mod:`newspulse.pitch` — ``_reference``, ``_radar_rows``, ``_radar_articles``,
``_spellings`` — and that is deliberate rather than sloppy. Each of them *is* a
definition: what "the mandate's radar" holds, what "the mandate's field" holds,
how one byline is spelled twice. Re-implementing any of them here would give the
dossier a second definition, silently different from the one the scan and the
pitch list already use, and the page's whole claim is that its figures resolve
to the same rows those pages resolve to.
"""

from __future__ import annotations

import collections
import datetime as dt
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import (
    newsjack,
    pitch,
    profile,
    reputation,
    stakeholders,
    stories,
    visibility,
)
from .models import (
    Angle,
    Article,
    Asset,
    CheckState,
    Client,
    NewsjackOpportunity,
    Outreach,
    OutreachReply,
    ReputationState,
    Standing,
)

# --- The urgency number (DEC-2) ---------------------------------------------------
#
# Three stored quantities, weighted so the sum is 0..100 and a consultant can
# re-derive it on paper. The weights are the decision's, spelled here so the
# badge and the provenance bar cannot disagree about them.

#: Outlets past which more outlets say nothing new. Eight: a story on eight
#: mastheads is a wave, and the ninth does not change what to do about it.
_MEDIA_CAP = 8
#: Reach, deadline and standing, in points. They sum to 100 by construction.
_REACH_POINTS = 40
_DEADLINE_POINTS = 40
_STANDING_POINTS = 20


def _half_up(value: float) -> int:
    """Round half up. Only ever called on a non-negative value.

    Python's ``round`` is banker's rounding, so ``round(17.5)`` and
    ``round(18.5)`` differ in a way nobody re-deriving the badge on paper would
    predict. The number on this page has to survive being checked by hand.

    Half *up* and not half away from zero, which are the same rule until the
    argument is negative: ``int(-2.5 + 0.5)`` is ``-2``. Both callers here feed
    it a share of a cap, so neither can, and the name says what it does rather
    than what a sign convention nobody exercises would do.
    """
    return int(value + 0.5)


@dataclass(frozen=True, slots=True)
class Urgency:
    """The badge's number, with the three addends it is made of.

    The badge shows :attr:`total` alone, the way the mock does. Everything else
    here exists so the same number can be written out beside it and in the
    provenance bar — a score whose parts are not on the page is a score nobody
    can argue with, which is the opposite of useful.
    """

    #: Distinct outlets carrying the story, as counted when it was weighed.
    media: int
    #: Whole hours of the window still to run, floored, never negative.
    hours_left: int
    #: The window's full width in hours, from the origin piece.
    hours_total: int
    reach: int
    deadline: int
    standing: int

    @property
    def total(self) -> int:
        return self.reach + self.deadline + self.standing

    @property
    def consumed(self) -> int:
        """Whole hours of the window already spent — the deadline's own input."""
        return max(0, self.hours_total - self.hours_left)


def urgency(
    opportunity: NewsjackOpportunity, *, now: dt.datetime | None = None
) -> Urgency:
    """The urgency of this opportunity, 0 to 100, per DEC-2.

    Reach rises with the outlets carrying it and stops at :data:`_MEDIA_CAP`.
    The deadline share rises as the window is used up — a story with two hours
    left is more urgent than the same story yesterday. Standing is the flat
    twenty a ``belegt`` verdict is worth, and nothing else scores it: the other
    two verdicts never become an opportunity at all.
    """
    reference = newsjack._reference(now)
    span = opportunity.window_ends_at - opportunity.article.published_at
    total_seconds = max(span.total_seconds(), 1.0)
    left = max(dt.timedelta(0), opportunity.window_ends_at - reference)
    used = min(1.0, max(0.0, 1.0 - left.total_seconds() / total_seconds))
    media = max(0, opportunity.pickup_count)
    return Urgency(
        media=media,
        hours_left=int(left.total_seconds() // 3600),
        hours_total=int(total_seconds // 3600),
        reach=_half_up(min(media, _MEDIA_CAP) / _MEDIA_CAP * _REACH_POINTS),
        deadline=_half_up(used * _DEADLINE_POINTS),
        standing=(
            _STANDING_POINTS if opportunity.standing is Standing.BELEGT else 0
        ),
    )


# --- The status the head shows ----------------------------------------------------


class Status(StrEnum):
    """What state the head's picker shows — derived, never stored.

    Deliberately not a column. "Aktiv" is not a thing anybody declares; it is
    what having opened the occasion *means*, and a second column asserting it
    would be free to disagree with the ``angles`` row that actually exists.
    """

    OFFEN = "offen"
    AKTIV = "aktiv"
    AUSGELAUFEN = "ausgelaufen"
    VERWORFEN = "verworfen"


#: The word each state wears in the head's control. A mapping rather than
#: ``value.capitalize()``, because the two are different kinds of thing: the
#: value is what the markup and the tests pin, the word is chrome GEL-03 may
#: reword without touching a state machine.
_STATUS_WORDS: dict[Status, str] = {
    Status.OFFEN: "Offen",
    Status.AKTIV: "Aktiv",
    Status.AUSGELAUFEN: "Ausgelaufen",
    Status.VERWORFEN: "Verworfen",
}

#: The four segments in the order the control draws them, built by walking the
#: enum — so a fifth state cannot be added without either giving it a word here
#: or failing at import, which is the whole reason the template no longer types
#: the four names out as strings.
STATUS_LABELS: tuple[tuple[Status, str], ...] = tuple(
    (state, _STATUS_WORDS[state]) for state in Status
)


def status(
    opportunity: NewsjackOpportunity,
    occasion: Angle | None,
    *,
    now: dt.datetime | None = None,
) -> Status:
    """The opportunity's state, read off the rows that exist.

    Order matters: an ending beats a beginning. A waved-off opportunity that had
    an occasion is *verworfen*, not *aktiv* — the consultant's last act on it is
    the one that describes it.
    """
    if opportunity.dismissed_at is not None:
        return Status.VERWORFEN
    if newsjack.is_expired(opportunity, now=now):
        return Status.AUSGELAUFEN
    return Status.AKTIV if occasion is not None else Status.OFFEN


def is_concluded(state: Status) -> bool:
    """Whether the page drops its action buttons and names the ending instead."""
    return state in (Status.AUSGELAUFEN, Status.VERWORFEN)


# --- The story's own pieces -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Source:
    """One piece carrying the story: the outlet, the moment, the stored byline."""

    outlet: str
    published_at: dt.datetime
    title: str
    url: str
    #: The byline as the feed carried it — ``""`` where none was stored. Never
    #: filled with a guess: most German feeds omit it, and a plausible name is
    #: worse than a named gap.
    author: str
    #: The story's earliest piece, the one the window and the row hang on.
    is_origin: bool


def _story_members(
    session: Session, opportunity: NewsjackOpportunity
) -> list[Article]:
    """Every article carrying this story, oldest first.

    The row anchors the origin piece only, so the pickups are re-derived here:
    the mandate's stored radar, run back through the same retrieval and the same
    clusterer the scan used. A pure read; nothing about it can write or fetch.

    Re-derived is not the same as reproduced, and the difference is worth saying
    out loud rather than claiming parity. ``newsjack.scan`` reads back twice the
    window from *scan time*; this reads back twice the window from the *origin*,
    because scan time is not on the row. The origin can sit anywhere inside the
    scan's reach, so this window can hold radar rows that one did not, and a
    greedy clusterer fed a different row set can answer with a different set of
    members. What the badge counts is therefore the outlet count frozen on the
    row at the verdict, never ``len()`` of this — and the provenance bar names
    both numbers for exactly that reason.

    The origin is always in the answer, even when the radar rows it was
    clustered from have since aged out of the lookback: an opportunity whose
    sources list is empty would be a dossier about nothing.
    """
    origin = opportunity.article
    span = opportunity.window_ends_at - origin.published_at
    rows = newsjack._radar_rows(
        session,
        opportunity.client,
        since=origin.published_at - span,
    )
    for story in stories.cluster(rows):
        ids = {member.article.id for member in story.members}
        if origin.id in ids:
            return sorted(
                (member.article for member in story.members),
                key=lambda article: (article.published_at, article.id),
            )
    return [origin]


def sources(session: Session, opportunity: NewsjackOpportunity) -> list[Source]:
    """The story's pieces, oldest first, with the origin marked as such."""
    origin_id = opportunity.article_id
    return [
        Source(
            outlet=article.source,
            published_at=article.published_at,
            title=article.title,
            url=article.url,
            author=(article.author or "").strip(),
            is_origin=article.id == origin_id,
        )
        for article in _story_members(session, opportunity)
    ]


# --- What hangs on the occasion ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class Text:
    """One stored text on the occasion, with its version and its check state."""

    id: int
    kind: str
    title: str
    generated_at: dt.datetime
    edited_at: dt.datetime | None
    #: Which draft of this format it is on this occasion — first is 1. Derived
    #: from the stored order rather than a column, because that is what the
    #: order *is*: the nth text of a kind on one occasion is its nth version.
    version: int
    check_state: CheckState
    released_at: dt.datetime | None


def occasion(session: Session, opportunity: NewsjackOpportunity) -> Angle | None:
    """The occasion this opportunity was opened as, if the button was pressed.

    Ordered by id for the reason ``assets_view._newsjack_occasion`` orders by
    it: two readers of one relation that sort differently can name different
    rows on a database without the partial unique index.
    """
    return session.scalars(
        select(Angle)
        .where(
            Angle.client_id == opportunity.client_id,
            Angle.newsjack_id == opportunity.id,
        )
        .order_by(Angle.id)
    ).first()


def texts(session: Session, occasion_row: Angle | None) -> list[Text]:
    """The texts hanging on the occasion, oldest first, each with its version."""
    if occasion_row is None:
        return []
    rows = session.scalars(
        select(Asset)
        .where(Asset.angle_id == occasion_row.id)
        .order_by(Asset.generated_at, Asset.id)
    ).all()
    seen: dict[str, int] = {}
    out: list[Text] = []
    for row in rows:
        seen[row.kind] = seen.get(row.kind, 0) + 1
        out.append(
            Text(
                id=row.id,
                kind=row.kind,
                title=row.title,
                generated_at=row.generated_at,
                edited_at=row.edited_at,
                version=seen[row.kind],
                check_state=row.check_state,
                released_at=row.released_at,
            )
        )
    return out


def letters(session: Session, occasion_row: Angle | None) -> list[Outreach]:
    """The letters written off this occasion, oldest first."""
    if occasion_row is None:
        return []
    return list(
        session.scalars(
            select(Outreach)
            .where(Outreach.angle_id == occasion_row.id)
            .order_by(Outreach.generated_at, Outreach.id)
        ).all()
    )


# --- The next step, read off the state (never guessed) ----------------------------


class StepKind(StrEnum):
    """The four moves the stored rows can call for, in the order they unblock."""

    TEXT = "text"
    FORMAT = "format"
    PRUEFUNG = "pruefung"
    SENDEN = "senden"


#: The chain, in order. A step's rank in it is what decides whether it blocks
#: another one — which is the whole of the hoch/mittel rule below.
_STEP_CHAIN: tuple[StepKind, ...] = (
    StepKind.TEXT,
    StepKind.FORMAT,
    StepKind.PRUEFUNG,
    StepKind.SENDEN,
)

_STEP_LABELS: dict[StepKind, str] = {
    StepKind.TEXT: "Text schreiben",
    StepKind.FORMAT: "Format wählen",
    StepKind.PRUEFUNG: "Prüfung starten",
    StepKind.SENDEN: "Senden",
}


@dataclass(frozen=True, slots=True)
class Step:
    """One recommended move, with why it is recommended and how hard it presses."""

    kind: StepKind
    label: str
    #: One sentence naming the stored rows this was read off.
    why: str
    #: ``"hoch"`` when another step waits behind this one, ``"mittel"`` when it
    #: is the last. Not a judgement about importance — a reading of the chain.
    weight: str


def _weight(kind: StepKind) -> str:
    """Whether this step blocks another one. That is the whole rule."""
    return "mittel" if kind is _STEP_CHAIN[-1] else "hoch"


def _current_step(
    occasion_row: Angle | None,
    stored_texts: list[Text],
    released_letters: int,
) -> tuple[StepKind, str] | None:
    """Which step the stored rows call for, and the sentence that says why.

    Four states and no fifth. Everything done means ``None`` — a tile inventing
    a next move once there is nothing left to do would be the same guess this
    module exists to refuse.
    """
    if occasion_row is None:
        return StepKind.TEXT, "Zu dieser Gelegenheit ist noch kein Anlass angelegt."
    if not stored_texts:
        return (
            StepKind.FORMAT,
            "Der Anlass steht, es hängt aber noch kein Text daran.",
        )
    unchecked = [t for t in stored_texts if t.check_state is CheckState.UNGEPRUEFT]
    if unchecked:
        return (
            StepKind.PRUEFUNG,
            f"{len(unchecked)} von {len(stored_texts)} Texten ist ungeprüft. "
            "Ohne Prüfung kann kein Text freigegeben werden.",
        )
    objected = [t for t in stored_texts if t.check_state is CheckState.EINWAND]
    checked = [t for t in stored_texts if t.check_state is CheckState.GEPRUEFT]
    if not checked:
        return (
            StepKind.PRUEFUNG,
            f"{len(objected)} von {len(stored_texts)} Texten trägt einen Einwand "
            "und keiner ist ohne Einwand geprüft.",
        )
    if released_letters == 0:
        return (
            StepKind.SENDEN,
            f"{len(checked)} geprüfte(r) Text(e) liegen vor, "
            "und es ist noch kein Brief freigegeben.",
        )
    return None


def steps(
    occasion_row: Angle | None,
    stored_texts: list[Text],
    stored_letters: list[Outreach],
) -> list[Step]:
    """The next step first, then the ones that come after it.

    The tail is the same chain read forward, so "danach" names real moves rather
    than a wish list — and it stops where the chain does.
    """
    released = sum(1 for letter in stored_letters if letter.released_at is not None)
    current = _current_step(occasion_row, stored_texts, released)
    if current is None:
        return []
    kind, why = current
    ordered = [Step(kind=kind, label=_STEP_LABELS[kind], why=why, weight=_weight(kind))]
    for later in _STEP_CHAIN[_STEP_CHAIN.index(kind) + 1 :]:
        ordered.append(
            Step(kind=later, label=_STEP_LABELS[later], why="", weight=_weight(later))
        )
    return ordered


# --- Who to approach (DEC-5) ------------------------------------------------------
#
# A fit of 0..100 out of three stored quantities and no model call. The weights
# are the decision's ordering made arithmetic: having written this very story
# outranks everything, a beat in the field is the second signal, and a contact
# on file is what makes either of them actionable today.

#: Wrote one of the pieces carrying this story. The strongest signal there is.
_FIT_WROTE_THE_STORY = 50
#: Per piece in the field over the last 90 days, up to :data:`_FIT_FIELD_CAP`.
_FIT_PER_FIELD_PIECE = 5
#: Beyond six pieces a quarter the byline is plainly on the beat; a seventh
#: says nothing further, and the cap keeps the field term at 30 points.
_FIT_FIELD_CAP = 6
#: A contact on file. Not about the person's relevance — about whether the
#: recommendation can be acted on this morning.
_FIT_HAS_CONTACT = 20


@dataclass(frozen=True, slots=True)
class Recipient:
    """One suggested journalist, with the fit and the three facts behind it."""

    name: str
    outlet: str
    #: Where the recommendation comes from, in ``pitch``'s own words.
    reason: str
    fit: int
    wrote_the_story: bool
    #: Pieces of *this mandate's* field this byline wrote in the last 90 days —
    #: uncapped here, capped only where it turns into points. See
    #: :func:`_field_presence` for why the count is the mandate's radar and not
    #: the byline's whole output.
    field_pieces: int
    #: Whether the contact book holds an entry. The address itself is never
    #: carried here — see the ``pitch`` module docstring on derived addresses.
    has_contact: bool
    #: When a released letter for this occasion last reached them, if one did.
    already_pitched_at: dt.datetime | None


def _field_presence(
    session: Session, client: Client, *, now: dt.datetime
) -> collections.Counter[str]:
    """How many pieces of *this mandate's field* each stored byline wrote, over
    :data:`pitch.LOOKBACK_DAYS`, keyed by the folded name.

    DEC-5's second quantity is presence "im Feld" — this mandate's field, not
    the byline's whole output. ``pitch.recent_headlines`` folds the two feed
    spellings of a name but filters on the author alone, so a journalist busy on
    an unrelated mandate's beat would score the field term here without ever
    having written about this one.

    So the count walks the same rows :func:`pitch.targets_for` builds the list
    from — ``TopicHit`` for this mandate, run through its own theme matcher —
    and the two private helpers are called rather than re-implemented for the
    reason the module docstring gives about ``newsjack``: a second definition of
    "the mandate's field" is a second answer nobody reconciles.

    One pass for the whole page rather than a query per recipient: the list runs
    to seventeen names and the field is one bounded read.
    """
    counts: collections.Counter[str] = collections.Counter()
    since = now - dt.timedelta(days=pitch.LOOKBACK_DAYS)
    for article in pitch._radar_articles(session, client, since):
        author = (article.author or "").strip()
        if author:
            counts[author.casefold()] += 1
    return counts


def _pieces_in_field(counts: collections.Counter[str], journalist: str) -> int:
    """This byline's pieces in the field, both feed spellings folded together.

    A set, because a single-word byline spells the same way twice and would
    otherwise be counted twice.
    """
    return sum(counts[spelling] for spelling in set(pitch._spellings(journalist)))


def _fit(target: pitch.PitchTarget, *, wrote: bool, field_pieces: int) -> int:
    """The three addends, summed. Capped at 100 by the weights themselves."""
    return (
        (_FIT_WROTE_THE_STORY if wrote else 0)
        + min(field_pieces, _FIT_FIELD_CAP) * _FIT_PER_FIELD_PIECE
        + (_FIT_HAS_CONTACT if target.contact_id is not None else 0)
    )


def recipients(
    session: Session,
    client: Client,
    occasion_row: Angle | None,
    story_authors: set[str],
    *,
    now: dt.datetime | None = None,
) -> list[Recipient]:
    """The media list for this opportunity, best fit first.

    The list itself comes from :mod:`newspulse.pitch` — the same one the letter
    is addressed from, so the dossier can never recommend somebody the outreach
    page would not. Only entries naming a *person* are kept: DEC-5 scores a
    journalist, and an outlet with no byline has none of the three quantities.
    A mandate whose field carries no stored byline therefore gets an empty list,
    which the page renders as the named gap it is.
    """
    reference = now or dt.datetime.now(dt.UTC)
    in_field = _field_presence(session, client, now=reference)
    out: list[Recipient] = []
    for target in pitch.targets_for(session, client, occasion_row, now=reference):
        if not target.journalist:
            continue
        wrote = target.journalist.strip().casefold() in story_authors
        field_pieces = _pieces_in_field(in_field, target.journalist)
        out.append(
            Recipient(
                name=target.journalist,
                outlet=target.outlet,
                reason=target.reason,
                fit=_fit(target, wrote=wrote, field_pieces=field_pieces),
                wrote_the_story=wrote,
                field_pieces=field_pieces,
                has_contact=target.contact_id is not None,
                already_pitched_at=target.already_pitched_at,
            )
        )
    # Stable, so ``pitch``'s own ordering settles a tie rather than the name.
    out.sort(key=lambda row: -row.fit)
    return out


# --- The trail (Entscheidungsspur) ------------------------------------------------


@dataclass(frozen=True, slots=True)
class Event:
    """One dated line of the trail: when, what, and on whose authority.

    ``ours`` separates what the market did from what the agency did, which is
    the distinction the whole strip exists to draw.
    """

    at: dt.datetime
    what: str
    who: str
    ours: bool


def _text_events(stored_texts: list[Text]) -> list[Event]:
    """A line per stored text — its format, its version, its check state — plus
    one more wherever a person has been through it."""
    states = {
        CheckState.UNGEPRUEFT: "ungeprüft",
        CheckState.EINWAND: "mit Einwand",
        CheckState.GEPRUEFT: "geprüft",
    }
    out: list[Event] = []
    for text in stored_texts:
        out.append(
            Event(
                at=text.generated_at,
                what=f"{text.kind} v{text.version}",
                who=states[text.check_state],
                ours=True,
            )
        )
        if text.edited_at is not None:
            out.append(
                Event(
                    at=text.edited_at,
                    what=f"{text.kind} v{text.version} bearbeitet",
                    who="Mensch",
                    ours=True,
                )
            )
    return out


def _letter_events(
    session: Session, stored_letters: list[Outreach]
) -> list[Event]:
    """A line per released letter, and one per reply that came back."""
    out: list[Event] = []
    for letter in stored_letters:
        if letter.released_at is None:
            continue
        out.append(
            Event(
                at=letter.released_at,
                what=f"Brief an {letter.journalist or letter.outlet} freigegeben",
                who=letter.released_by or "Mensch",
                ours=True,
            )
        )
    ids = [letter.id for letter in stored_letters]
    if not ids:
        return out
    replies = session.scalars(
        select(OutreachReply)
        .where(OutreachReply.outreach_id.in_(ids))
        .order_by(OutreachReply.received_at)
    ).all()
    out.extend(
        Event(at=reply.received_at, what="Antwort", who=reply.sender, ours=False)
        for reply in replies
    )
    return out


def trail(
    session: Session,
    opportunity: NewsjackOpportunity,
    story: list[Source],
    occasion_row: Angle | None,
    stored_texts: list[Text],
    stored_letters: list[Outreach],
) -> list[Event]:
    """The story's pieces and the agency's own steps, in one time-sorted row.

    Every line comes off a stored timestamp. Nothing here is a second record of
    the same events — which is why there is no event table: two truths about one
    morning drift, and the one that gets maintained is never the one being read.
    """
    events: list[Event] = [
        Event(
            at=source.published_at,
            what=f"{source.outlet}{' (Ursprung)' if source.is_origin else ''}",
            who=source.author or source.outlet,
            ours=False,
        )
        for source in story
    ]
    version = opportunity.brain_version
    events.append(
        Event(
            at=opportunity.created_at,
            what="Gelegenheit erkannt",
            who="Schnelle Spur",
            ours=True,
        )
    )
    events.append(
        Event(
            at=opportunity.created_at,
            what="Stehen geprüft",
            who=(
                f"Standards v{version}" if version is not None else "Standards unbekannt"
            ),
            ours=True,
        )
    )
    if occasion_row is not None:
        events.append(
            Event(
                at=occasion_row.generated_at,
                what="Anlass geöffnet",
                who="Mensch",
                ours=True,
            )
        )
    events.extend(_text_events(stored_texts))
    events.extend(_letter_events(session, stored_letters))
    if opportunity.dismissed_at is not None:
        events.append(
            Event(
                at=opportunity.dismissed_at,
                what="Gelegenheit verworfen",
                who="Mensch",
                ours=True,
            )
        )
    events.sort(key=lambda event: event.at)
    return events


# --- The forward look (DEC-3), read and never made --------------------------------


@dataclass(frozen=True, slots=True)
class Outlook:
    """The stored look-ahead, and when it was made.

    ``entries`` empty is the ordinary case and a statement in its own right: no
    estimate was made at detection. The tile says so; it does not draw an empty
    calendar, and it never makes one on the way past.
    """

    entries: tuple[dict[str, str], ...]
    made_at: dt.datetime | None

    @property
    def exists(self) -> bool:
        return bool(self.entries)


def outlook(opportunity: NewsjackOpportunity) -> Outlook:
    """The forward look as stored with the verdict. A read, nothing else."""
    entries = tuple(
        {"when": str(entry.get("when", "")), "what": str(entry.get("what", ""))}
        for entry in (opportunity.outlook or [])
        if isinstance(entry, dict) and entry.get("when") and entry.get("what")
    )
    return Outlook(entries=entries, made_at=opportunity.outlook_at if entries else None)


# --- The audience chips (DEC-6 A) -------------------------------------------------


@dataclass(frozen=True, slots=True)
class Audience:
    """One of the mandate's own stakeholder groups, as the chips under the
    storyline read them.

    DEC-6 A, and the wording of the chip follows from it: these are the groups
    the mandate works for, in the order its map keeps them — not a judgement
    that this story touches exactly them. The distinction matters, because the
    second claim needs a model call and nothing stored would back it up.
    """

    group: str
    #: How strongly the group is weighed on the mandate's own map.
    level: str


def audiences(session: Session, client: Client) -> list[Audience]:
    """The mandate's stakeholder groups, off the stored map (DEC-6 A).

    Read through :func:`newspulse.stakeholders.card`, so the chips and the map
    page can never show a different set or a different order. An empty map is
    an empty list, which the page renders as the named gap with the link to
    where the map is kept — never as a list of plausible groups in the template,
    which is the one thing DEC-6 rules out in both of its options.
    """
    return [
        Audience(group=row.group_name, level=row.einfluss.value)
        for row in stakeholders.card(session, client)
    ]


# --- The rail: what RauteOS already knows about the mandate (DEC-1 A) -------------
#
# The mock's right-hand column, and the reason it is on this page at all: the
# five questions a consultant answers from memory before deciding whether a
# story is worth the morning — is the positioning still current, can we reach
# anybody, do our texts hold up, are we visible on this cluster, and is the
# mandate quiet enough to go looking for coverage. Every one of them is already
# a stored row somewhere else in this tool, on five different pages.
#
# Each item carries figures rather than a sentence. The sentence is chrome and
# belongs in the template where :func:`newspulse.i18n.translate` reaches it; the
# figures are the stored rows, and putting them in a dataclass is what lets a
# test pin the number without parsing German. A named absence is a value here
# too — ``None`` and ``0`` mean different things, and every one of these has an
# empty state that is a statement rather than a gap.


@dataclass(frozen=True, slots=True)
class Positioning:
    """The mandate's own positioning line, and how fresh the check on it is."""

    #: Verbatim from the profile — data, so it stays in the language it was
    #: written in. Empty when the field has never been filled.
    value: str
    #: When the profile was last looked at, in the shape the page prints it.
    checked: profile.Checked


@dataclass(frozen=True, slots=True)
class MediaReach:
    """How much of the media list can actually be acted on this morning."""

    #: Bylines the list offers for this opportunity.
    named: int
    #: How many of them have a contact on file. The gap between the two is the
    #: item's whole point: a list of seventeen names and three addresses is a
    #: different morning from seventeen and seventeen.
    with_contact: int


@dataclass(frozen=True, slots=True)
class Quality:
    """What the two checkers have said about the texts on this occasion."""

    texts: int
    checked: int
    objected: int

    @property
    def unchecked(self) -> int:
        return self.texts - self.checked - self.objected


@dataclass(frozen=True, slots=True)
class Visibility:
    """Where the mandate stood in the last AI-visibility measurement."""

    #: When that measurement ran. ``None`` when the mandate has never been
    #: measured, which is not the same as having been measured and not named.
    measured_at: dt.datetime | None
    #: Readable answers in that run, and how many of them named the mandate.
    answers: int
    named_in: int
    #: The best rank it reached in any of them, or ``None`` where it was named
    #: without a position being read.
    best_position: int | None


@dataclass(frozen=True, slots=True)
class Reputation:
    """The mandate's newest reputation reading, and which way the series moves."""

    #: ``None`` when the sweep has never read this mandate — a state of its own,
    #: and emphatically not "ruhig", which is a measurement.
    state: ReputationState | None
    direction: reputation.Direction
    articles: int
    negative: int
    day: dt.date | None


@dataclass(frozen=True, slots=True)
class Intelligence:
    """The five items of the rail, in the order the mock stacks them."""

    positioning: Positioning
    media: MediaReach
    quality: Quality
    visibility: Visibility
    reputation: Reputation


#: How many readings the rail's direction is read over. The band's own constant,
#: so the arrow here and the arrow on Heute can never point different ways.
_TREND_READINGS = reputation.DIRECTION_READINGS


def _positioning(session: Session, client: Client, *, now: dt.datetime) -> Positioning:
    """The stored positioning line, with the profile's own freshness stamp."""
    facts = profile.stored(session, client.id)
    row = facts.get("positionierung")
    return Positioning(
        value=(row.value or "").strip() if row is not None else "",
        checked=profile.checked(client.profile_checked_at, now=now),
    )


def _quality(stored_texts: list[Text]) -> Quality:
    """The check states of the texts on this occasion, counted three ways.

    Three counts and not a share: "geprüft" and "nichts hat hingeschaut" are the
    two states :attr:`newspulse.models.Asset.check_state` exists to keep apart,
    and a percentage would fold them back together.
    """
    return Quality(
        texts=len(stored_texts),
        checked=sum(1 for row in stored_texts if row.check_state is CheckState.GEPRUEFT),
        objected=sum(1 for row in stored_texts if row.check_state is CheckState.EINWAND),
    )


def _visibility(session: Session, client: Client) -> Visibility:
    """The newest AI-visibility run, read as three counts and a best rank."""
    run = visibility.latest_run(session, client)
    if run is None:
        return Visibility(measured_at=None, answers=0, named_in=0, best_position=None)
    positions = [
        answer.position
        for answer in run.answers
        if answer.named and answer.position is not None
    ]
    return Visibility(
        measured_at=run.ran_at,
        answers=len(run.answers),
        named_in=sum(1 for answer in run.answers if answer.named),
        best_position=min(positions) if positions else None,
    )


def _reputation(session: Session, client: Client) -> Reputation:
    """The newest reputation reading and the direction of the ones behind it."""
    readings = reputation.history(session, client, limit=_TREND_READINGS)
    if not readings:
        return Reputation(
            state=None,
            direction=reputation.Direction.STABIL,
            articles=0,
            negative=0,
            day=None,
        )
    newest = readings[0]
    return Reputation(
        state=newest.state,
        direction=reputation.direction(readings),
        articles=newest.articles,
        negative=newest.negative,
        day=newest.day,
    )


def intelligence(
    session: Session,
    client: Client,
    people: list[Recipient],
    stored_texts: list[Text],
    *,
    now: dt.datetime | None = None,
) -> Intelligence:
    """The rail beside the tiles: five stored answers, no model call, no write.

    The two that are already on the page — the media list and the texts — are
    passed in rather than fetched again, so the rail and the tiles beside it
    cannot disagree about the same figure. The other three are one read each.
    """
    reference = now or dt.datetime.now(dt.UTC)
    return Intelligence(
        positioning=_positioning(session, client, now=reference),
        media=MediaReach(
            named=len(people),
            with_contact=sum(1 for person in people if person.has_contact),
        ),
        quality=_quality(stored_texts),
        visibility=_visibility(session, client),
        reputation=_reputation(session, client),
    )


# --- Where every number on the page came from -------------------------------------


@dataclass(frozen=True, slots=True)
class Provenance:
    """The bar above the tiles: what found this, what scored it, what checked it."""

    #: How the opportunity was detected, in one line.
    detected: str
    #: The urgency's three inputs, written out.
    scored: str
    #: Which version of the house standards the standing was checked against.
    checked_against: str


def provenance(
    opportunity: NewsjackOpportunity, score: Urgency, pieces: int
) -> Provenance:
    """The three sentences under which every figure on the page was produced.

    Two counts appear here and they are deliberately not the same number.
    ``score.media`` is the distinct *outlets* counted when the verdict was
    written and frozen on the row — that is the score's input, so the bar has
    to name it or the badge cannot be checked. ``pieces`` is how many articles
    of the story are still readable today, which is what the sources list can
    actually show. They diverge when radar rows age past the lookback, and a
    page printing one under the other's name would be lying about both.

    Which is also why the sentence does not say "davon": four articles can come
    off three mastheads, so the pieces are not a subset of the outlets and a
    partitive would read as a contradiction on the one bar whose job is to keep
    the two apart.
    """
    version = opportunity.brain_version
    return Provenance(
        detected=(
            f"Schnelle Spur: {score.media} Medien trugen die Story bei der "
            f"Prüfung, zuerst bei {opportunity.article.source}; "
            f"{pieces} Beitrag/Beiträge der Story sind heute gespeichert."
        ),
        scored=(
            f"Verbreitung {score.reach} ({score.media} Medien, gedeckelt bei "
            f"{_MEDIA_CAP}) · Frist {score.deadline} ({score.consumed} von "
            f"{score.hours_total} Std verbraucht) · Stehen {score.standing}"
        ),
        checked_against=(
            f"Standards, Fassung v{version}"
            if version is not None
            else "Standards, Fassung nicht vermerkt"
        ),
    )


__all__ = [
    "STATUS_LABELS",
    "Audience",
    "Intelligence",
    "Event",
    "MediaReach",
    "Outlook",
    "Positioning",
    "Provenance",
    "Quality",
    "Recipient",
    "Reputation",
    "Source",
    "Status",
    "Step",
    "StepKind",
    "Text",
    "Urgency",
    "Visibility",
    "audiences",
    "intelligence",
    "is_concluded",
    "letters",
    "occasion",
    "outlook",
    "provenance",
    "recipients",
    "sources",
    "status",
    "steps",
    "texts",
    "trail",
    "urgency",
]
