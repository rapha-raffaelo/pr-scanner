"""The personalised message: one impulse, written at one recipient.

Why this exists
---------------
The Impulse page carried two panels. One held a positioning draft built from the
market; the other held "recommendations" built from the mandate's own press. The
consultant's verdict on the pair was short: *"das ist wirklich nicht ganz klar wo
der unterschied liegt"*. He was right, and the reason is that only one of them
was a thing you can do. A position is not an action. Sending it to a named
journalist is.

So the second panel is gone and its substance moved here. What the recommendation
had that the impulse lacked — the mandate's own coverage, the reason a reporter
should care about *this* company — is exactly what personalises a pitch. It is
the same material, doing work instead of describing it.

What it is not
--------------
Nothing is sent. This writes a draft into a card with a copy button, and the
consultant decides. That posture is the same one every generated text in this
tool holds, and it is not negotiable: he is the accountable party.

No contact details are invented, here or anywhere. The recipient's name and
outlet come from a byline the feed actually carried or from the contact book the
consultant filled in himself; the model is told to sign with the mandate's name
and nothing else, because a plausible invented signature is worse than none.

The ledger
----------
What the tool never learned was whether any of it went out. :func:`release`
records that a person read a letter and let it go, :func:`record_outcome` records
what came back, and :func:`is_silent` derives the one state nobody enters. From
the moment a letter is released its text is frozen: :func:`store` overwrites only
a draft, so a redraft for the same recipient becomes a new row rather than
destroying the record of what was actually sent.

One honest caveat on the word "record": the routes that write these rows share
the app's trust boundary, which is HTTP Basic behind a loopback bind by default
and — like every POST in this tool — no CSRF token yet. The two ledger routes
at least refuse a browser POST whose ``Origin``/``Referer`` names a foreign
page (see ``advisory._refuse_foreign_origin``), so another site cannot mint a
release through the consultant's browser; a proper same-site token remains an
app-wide task, and the ledger's claim is only as strong as that boundary.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from importlib import resources
from string import Template
from typing import TYPE_CHECKING

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from . import config, contacts, gemini, guide, prose
from .analyzer import ParseError, invoke_with_fallback, strip_code_fence
from .models import (
    SILENT_AFTER_DAYS,
    Analysis,
    Angle,
    Article,
    Client,
    Outreach,
    OutreachState,
    visible_coverage,
)
from .pitch import PitchTarget
from .schemas import MessageReview, PersonalMessage

if TYPE_CHECKING:
    # Only for the two signatures below. The ledger records *that* a letter went
    # out and when; how it was reached is the transport's business, and importing
    # the Gmail client at runtime to write a type would make this module depend
    # on it for nothing.
    from . import gmail_link

_log = logging.getLogger(__name__)

_PROMPT_RESOURCE = "prompts/outreach.txt"

#: No user accounts exist in this tool, so a release is signed the way a
#: hand-filled profile fact is: by the only distinction that matters here, which
#: is that a person and not the machine did it. See ``ClientFact.filled_by``.
DEFAULT_RELEASED_BY = "mensch"

#: ``Outreach.released_by`` is ``String(80)``. SQLite stores a longer value
#: without complaint and any other backend raises, so the bound is enforced
#: here, where the value is written, rather than trusted to the column.
_RELEASED_BY_MAX = 80

#: How far back the mandate's own coverage counts as a reason for a journalist to
#: care. Longer than the angle prompt's week: a pitch may point at a piece from
#: last month ("Sie haben damals über … geschrieben"), where a positioning text
#: only needs to avoid repeating itself.
_OWN_COVERAGE_DAYS = 60
_MAX_OWN_COVERAGE = 6


def _prompt_template() -> Template:
    text = resources.files("newspulse").joinpath(_PROMPT_RESOURCE).read_text("utf-8")
    return Template(text)


def _client_profile(client: Client) -> str:
    parts = [f"Name: {client.name}"]
    if client.industry:
        parts.append(f"Branche: {client.industry}")
    if client.website:
        parts.append(f"Website: {client.website}")
    if client.keywords:
        parts.append(f"Themen: {', '.join(client.keywords)}")
    return "\n".join(parts)


def _recipient_block(target: PitchTarget | None) -> str:
    """Who this goes to — or, honestly, that nobody in particular does.

    A general version is a legitimate answer: most feeds carry no byline, and a
    consultant who knows the desk he wants can take the text and address it
    himself. What must not happen is the model inventing a name to fill the slot.
    """
    if target is None:
        return (
            "Kein konkreter Empfänger. Schreibe die Nachricht so, dass der Berater "
            "sie an eine Fachredaktion seiner Wahl schicken kann. Erfinde keinen "
            "Namen und kein Medium."
        )
    who = target.journalist or "Kein Name bekannt (nur die Redaktion)"
    lines = [f"Journalist/in: {who}", f"Medium: {target.outlet}"]
    if target.about_client == 0:
        lines.append(
            "Hat über diesen Mandanten noch nie geschrieben — das ist der Grund "
            "für den Pitch, aber kein Vorwurf und kein Thema der Nachricht."
        )
    else:
        lines.append(
            f"Hat in den letzten Monaten {target.about_client}× über den Mandanten "
            "geschrieben — die Nachricht darf daran anknüpfen."
        )
    return "\n".join(lines)


def _recipient_work(target: PitchTarget | None) -> str:
    """The recipient's own recent headlines, which is what makes a first line
    real rather than flattering."""
    if target is None or not target.evidence:
        return ""
    headlines = "\n".join(f"- {headline}" for headline in target.evidence)
    return (
        "WAS DIESE:R EMPFÄNGER:IN ZULETZT ZUM THEMENFELD GESCHRIEBEN HAT\n"
        "Nur diese Schlagzeilen sind belegt. Beziehe dich auf sie, nicht auf "
        "vermutete andere Beiträge.\n"
        f"{headlines}\n"
    )


def _own_coverage_block(session: Session, client_id: int) -> str:
    """The mandate's own press — the half the old "Empfehlung" panel worked from.

    Here it earns its keep: it is the evidence that this company is a subject the
    press already takes seriously, which is what a journalist weighs before
    answering a stranger.
    """
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=_OWN_COVERAGE_DAYS)
    rows = session.execute(
        select(Article)
        .join(Analysis, Analysis.article_id == Article.id)
        .where(
            Analysis.client_id == client_id,
            visible_coverage(),
            Article.published_at >= since,
        )
        .order_by(Analysis.importance_score.desc(), Article.published_at.desc())
        .limit(_MAX_OWN_COVERAGE)
    ).all()
    if not rows:
        return (
            "BERICHTERSTATTUNG ÜBER DEN MANDANTEN\n"
            "Keine in den letzten Monaten. Die Nachricht darf also nicht so tun, "
            "als sei der Mandant bekannt — sie muss allein über die Sache tragen.\n"
        )
    headlines = "\n".join(f"- ({a.source}): {a.title}" for (a,) in rows)
    return (
        f"BERICHTERSTATTUNG ÜBER DEN MANDANTEN, LETZTE {_OWN_COVERAGE_DAYS} TAGE\n"
        "Beleg dafür, dass er ein Thema ist. Höchstens eine Erwähnung, nie eine "
        "Aufzählung.\n"
        f"{headlines}\n"
    )


def _parse(raw: str) -> PersonalMessage:
    """Validate the reply into a message; anything else is a ParseError.

    Same trust boundary as everywhere else in this codebase: the reply is text
    until the schema says otherwise. A fence is unwrapped because wrapping JSON in
    ```json is a habit rather than an error.
    """
    try:
        payload = json.loads(strip_code_fence(raw))
    except json.JSONDecodeError as exc:
        raise ParseError(f"outreach was not valid JSON: {exc}") from exc
    try:
        message = PersonalMessage.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 — pydantic raises its own type
        raise ParseError(f"outreach did not match the schema: {exc}") from exc
    if not message.message.strip():
        raise ParseError("outreach carried no message")
    return message


def draft(
    session: Session,
    client: Client,
    angle: Angle,
    target: PitchTarget | None = None,
    *,
    invoke=invoke_with_fallback,
) -> PersonalMessage:
    """Write the message for ``angle``, aimed at ``target`` if there is one.

    Unlike :func:`newspulse.angles.suggest` there is no "nothing to say" outcome.
    The judgement of whether there is an opening was already made — it is the
    impulse. This one was asked for by a person looking at that impulse, and
    answering "no" to a direct request is not honesty here, it is a broken button.
    A backend failure still raises, because "the draft failed" and "here is your
    draft" must never look alike.
    """
    prompt = _prompt_template().substitute(
        client_profile=_client_profile(client),
        comms_guide=guide.for_prompt(client),
        thesis=angle.thesis or "—",
        overclaim=angle.overclaim or "—",
        angle_message=angle.message,
        context=angle.context or "—",
        recipient=_recipient_block(target),
        recipient_work=_recipient_work(target),
        own_coverage=_own_coverage_block(session, client.id),
    )
    return _parse(invoke(prompt, timeout=config.ANALYZER_TIMEOUT))


_CROSSCHECK_RESOURCE = "prompts/crosscheck.txt"


def crosscheck(
    session: Session,
    client: Client,
    angle: Angle,
    message: PersonalMessage,
    target: PitchTarget | None = None,
    *,
    generate=None,
) -> tuple[MessageReview, str]:
    """Have a *different* model read the letter, and say which one did.

    The model that wrote a pitch cannot judge whether it oversells: it chose every
    word for a reason it still believes, and asking it to review its own work
    reliably produces "looks good". So this runs on the configured second provider
    — Gemini, with its own key — and is asked one narrow question: would this
    embarrass the sender.

    Returns the review and the name of the model that gave it. Raises
    :class:`RuntimeError` when no second model is configured, because a check that
    silently did not happen is worse than no check at all: the page would show a
    letter with no objections and the reader would take that for a verdict.

    ``generate`` is injectable so the tests drive the whole path without a network
    call; by default it is :func:`newspulse.gemini.generate`, which is deliberately
    *not* the fallback-wrapped invoker the drafting side uses — falling back to
    Claude here would quietly turn the cross-check into a self-check.
    """
    if generate is None:
        if not config.review_configured():
            raise RuntimeError(
                "Kein Zweitmodell hinterlegt: GEMINI_API_KEY (oder "
                "NEWSPULSE_GEMINI_API_KEY) in der .env setzen, damit ein anderes "
                "Modell die Nachricht gegenliest."
            )

        def generate(prompt: str, **kwargs) -> str:
            return gemini.generate(
                prompt,
                model=config.review_model(),
                api_key=config.review_api_key(),
                **kwargs,
            )

    template = Template(
        resources.files("newspulse").joinpath(_CROSSCHECK_RESOURCE).read_text("utf-8")
    )
    prompt = template.substitute(
        client=client.name,
        thesis=angle.thesis or "—",
        overclaim=angle.overclaim or "—",
        recipient=_recipient_block(target),
        recipient_work=_recipient_work(target) or "Keine belegten Schlagzeilen.",
        own_coverage=_own_coverage_block(session, client.id),
        subject=message.subject,
        message=message.message,
    )
    raw = generate(prompt)
    try:
        payload = json.loads(strip_code_fence(raw))
        review = MessageReview.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 — pydantic and json raise their own
        raise ParseError(f"crosscheck did not match the schema: {exc}") from exc

    # One thing the checker cannot be trusted to catch, because it is mechanical:
    # the house rule on dashes. Checked here rather than believed.
    if prose.has_dash(message.message) or prose.has_dash(message.subject):
        review = review.model_copy(
            update={
                "concerns": [
                    *review.concerns,
                    "Gedankenstrich im Text — verrät maschinelles Schreiben.",
                ][:5]
            }
        )
    return review, config.review_model()


def _message_fields(
    message: PersonalMessage, review: MessageReview | None, reviewed_by: str
) -> dict:
    """The columns one drafted text writes, shared by both of ``store``'s paths."""
    # A stored review always belongs to the text beside it: re-writing for the
    # same recipient clears the old verdict rather than letting it stand over a
    # letter it never read.
    review_text = "\n".join(review.concerns) if review else ""
    if review and review.fix:
        review_text = f"{review_text}\nZuerst ändern: {review.fix}".strip()
    return {
        # House style, enforced rather than requested: the prompt asks for no
        # dashes and the model relapses by the third paragraph. See newspulse.prose.
        "subject": prose.plain(message.subject),
        "message": prose.plain(message.message),
        "hook": message.hook.strip(),
        "review": review_text,
        "reviewed_by": reviewed_by if review else "",
        "review_ok": review.send if review else True,
        "generated_at": dt.datetime.now(dt.UTC),
    }


def store(
    session: Session,
    client: Client,
    angle: Angle,
    message: PersonalMessage,
    target: PitchTarget | None = None,
    review: MessageReview | None = None,
    reviewed_by: str = "",
) -> Outreach:
    """Persist one message. Re-writing for the same recipient replaces the old
    one *while it is still a draft*: two drafts at the same journalist are two
    attempts, not two pitches.

    Once a letter has been released that upsert would be data loss, and of the
    worst kind: it would overwrite the only record of the text a journalist
    actually received with one nobody sent. So a released row is never touched
    here and the redraft becomes a new row beside it — and the "still a draft"
    test is re-checked by the database at write time, not only at the read
    above it, because a release may commit in between (the rewrite finishing
    while the consultant clicks the release button).
    """
    journalist = (target.journalist or "") if target else ""
    outlet = (target.outlet or "") if target else ""
    fields = _message_fields(message, review, reviewed_by)
    draft_row = session.scalars(
        select(Outreach)
        .where(
            Outreach.angle_id == angle.id,
            Outreach.journalist == journalist,
            Outreach.outlet == outlet,
            # A draft is both things at once: the state says so and no release is
            # stamped. Filtered on both because ``released_at`` is the field every
            # other release check keys on, and a later writer (OUT-04's mailbox
            # sync) may stamp it without walking through :func:`release` — such a
            # row must count as released here, whatever its state still says.
            Outreach.state == OutreachState.ENTWURF,
            Outreach.released_at.is_(None),
        )
        # At most one draft per recipient exists, but ordering makes which one
        # is meant a fact rather than a coincidence of insertion order.
        .order_by(Outreach.id.desc())
    ).first()
    if draft_row is not None:
        # The freeze holds in the database, not in the copy this session read:
        # the WHERE repeats the draft test so a release committed since the
        # SELECT makes this match zero rows instead of rewriting a released
        # letter's text — in which case the redraft becomes a new row below.
        claimed = session.execute(
            update(Outreach)
            .where(
                Outreach.id == draft_row.id,
                Outreach.state == OutreachState.ENTWURF,
                Outreach.released_at.is_(None),
            )
            .values(**fields)
            .execution_options(synchronize_session=False)
        ).rowcount
        session.commit()
        if claimed:
            session.refresh(draft_row)
            return draft_row
    row = Outreach(
        angle_id=angle.id,
        client_id=client.id,
        journalist=journalist,
        outlet=outlet,
        **fields,
    )
    session.add(row)
    session.commit()
    return row


# --- The ledger -----------------------------------------------------------------

#: What each state is called on screen. German, because the interface is; the
#: English side lives in :mod:`newspulse.i18n` like every other label. Kept here
#: rather than in the template so the badge, the outcome form and any later view
#: cannot drift into calling the same state two different things.
STATE_LABELS: dict[OutreachState, str] = {
    OutreachState.ENTWURF: "Entwurf",
    # Not "Freigegeben": what the consultant did was release *and* send it, and
    # the ledger's whole claim is about the letter having left the house.
    OutreachState.RAUS: "Verschickt",
    OutreachState.ANTWORT: "Antwort",
    OutreachState.ABSAGE: "Absage",
    OutreachState.VEROEFFENTLICHT: "Veröffentlicht",
}

#: The states a person may record as an outcome. ``ENTWURF`` and ``RAUS`` are not
#: outcomes: one is where a letter starts and the other is what releasing does.
#: Deliberately one-way: there is no path from an outcome back to plain
#: "verschickt", because taking a result off the record is a ledger question —
#: erase it or record the correction? — worth answering on purpose rather than
#: in passing. A typo is fixed by recording the right outcome over it.
OUTCOMES: tuple[OutreachState, ...] = (
    OutreachState.ANTWORT,
    OutreachState.ABSAGE,
    OutreachState.VEROEFFENTLICHT,
)


def release(
    session: Session, row: Outreach, released_by: str = "", *, at: dt.datetime | None = None
) -> Outreach:
    """Record that a person read this letter, released it and sent it.

    This is the moment the product's L5 claim — "ein Mensch liest, ändert, gibt
    frei" — stops being a description and becomes a row. Three things happen and
    the order of them is the point: the release is stamped, the recipient is
    resolved against the contact book, and the text is frozen from here on
    (:func:`store` will no longer overwrite it).

    Releasing twice is not an error and not a second release: the first timestamp
    stands. A double submit, a reloaded confirmation page or a second consultant
    clicking the same button must not rewrite when the decision was made — and
    the two clicks may *overlap*, each request reading the row unreleased in its
    own session. So the guard is not the read below but the UPDATE's own WHERE,
    which the database re-checks at write time: exactly one request stamps the
    row, and the other reads the winner's record back.

    ``at`` is for the one caller that knows better than this machine's clock when
    the letter actually left: Gmail states its own send date on the message, and
    that — not the moment this row was written — is when the journalist received
    it. It is ignored on a letter someone already released by hand, for the same
    reason a second click is: the first release stands.
    """
    if row.released_at is not None:
        return row
    # A consultant who types the literal token "mensch" into the form is stored
    # indistinguishably from the anonymous default and renders as the unsigned
    # trail line. Accepted: the token means "a person did this" either way.
    signer = ((released_by or "").strip() or DEFAULT_RELEASED_BY)[:_RELEASED_BY_MAX]
    # Resolved once, here, rather than matched again on every read. Left null
    # when the book has no entry: the letter still names its recipient in its own
    # ``journalist``/``outlet``, and a guessed link is worse than none.
    match = contacts.find(session, row.journalist, row.outlet)
    session.execute(
        update(Outreach)
        .where(Outreach.id == row.id, Outreach.released_at.is_(None))
        .values(
            released_at=at or dt.datetime.now(dt.UTC),
            released_by=signer,
            state=OutreachState.RAUS,
            contact_id=match.id if match is not None else None,
        )
        .execution_options(synchronize_session=False)
    )
    session.commit()
    # Whether this request won or lost the write, the row now shows the release
    # that actually stands.
    session.refresh(row)
    return row


def record_sent(
    session: Session,
    row: Outreach,
    sent: gmail_link.Sent,
    *,
    draft_id: str = "",
    released_by: str = "",
) -> Outreach:
    """Record that this letter left the house through Gmail.

    Two things happen and only one of them is new. The thread ids are Google's
    answer, stored verbatim — this is what turns OUT-05's reply matching from a
    guess into a fact. The release is the ordinary one: :func:`release` stamps it
    at *Gmail's* send date rather than at this machine's clock, and does nothing
    at all if a person already released the letter by hand, so a letter released
    on Monday and pushed to Gmail on Tuesday keeps Monday as the moment a human
    took responsibility for it.
    """
    row.gmail_draft_id = draft_id or row.gmail_draft_id
    row.gmail_thread_id = sent.thread_id
    row.gmail_message_id = sent.message_id
    session.add(row)
    session.commit()
    return release(session, row, released_by=released_by, at=sent.sent_at)


def record_draft(session: Session, row: Outreach, draft: gmail_link.Draft) -> Outreach:
    """Remember the draft Gmail composed, before anything has been sent.

    Written in its own commit rather than together with the send, because the
    send is the call that can fail after Gmail has already accepted the draft.
    Losing the id there would leave an orphan draft in the mailbox and make the
    next push compose a second one at the same journalist — which is exactly the
    duplicate the two-step exists to prevent.
    """
    row.gmail_draft_id = draft.draft_id
    # The draft already has a conversation; the message id stays empty until
    # something is actually sent, because that is what ``sent_through_gmail``
    # keys on and a composed draft is not a sent letter.
    row.gmail_thread_id = draft.thread_id
    session.add(row)
    session.commit()
    return row


def record_outcome(
    session: Session, row: Outreach, state: OutreachState | str, note: str = ""
) -> Outreach:
    """Record what came back. Raises :class:`ValueError` on anything else.

    Two refusals, both of them about not letting the ledger hold a sentence
    nobody could have said. A letter that was never released has no outcome —
    there is nothing it could be the answer to — and ``entwurf`` or ``raus`` are
    not results, so neither can be entered as one. Both raise *before* the row is
    touched, so a refused outcome leaves the letter exactly as it was.

    ``note`` is stored as typed, minus surrounding whitespace. It is the one text
    on this row a human wrote, so the house rule on dashes that
    :func:`store` enforces on generated prose has no business here.

    Unlike :func:`release`, ``outcome_at`` deliberately follows the *most recent*
    entry rather than the first: an outcome may lawfully change — a reply becomes
    a publication, a typo gets corrected — and the timestamp belongs to the
    outcome standing beside it, not to one it replaced.
    """
    if row.released_at is None:
        raise ValueError(
            "Ein Anschreiben, das nie freigegeben wurde, kann kein Ergebnis haben."
        )
    try:
        wanted = OutreachState(state)
    except ValueError as exc:
        raise ValueError(f"Unbekanntes Ergebnis: {state!r}") from exc
    if wanted not in OUTCOMES:
        raise ValueError(f"{STATE_LABELS[wanted]} ist kein Ergebnis.")
    row.state = wanted
    row.outcome_note = (note or "").strip()
    row.outcome_at = dt.datetime.now(dt.UTC)
    session.add(row)
    session.commit()
    return row


def is_silent(row: Outreach, *, now: dt.datetime | None = None) -> bool:
    """Whether this letter has been out for too long with nothing recorded.

    Derived rather than stored, which is the whole reason there is no "ohne
    Reaktion" state. Silence is an absence: nobody enters it, it becomes true on
    a day no one is looking, and it stops being true the moment an outcome
    arrives. A stored version would need a nightly job to keep a claim about
    nothing up to date, and would be wrong between two runs.
    """
    if row.state != OutreachState.RAUS or row.released_at is None:
        return False
    moment = now or dt.datetime.now(dt.UTC)
    # ``>=`` on purpose: the moment the fourteenth day has fully elapsed counts
    # as "after 14 days" — the card should not wait for day fifteen to say so.
    return moment - row.released_at >= dt.timedelta(days=SILENT_AFTER_DAYS)


def days_out(row: Outreach, *, now: dt.datetime | None = None) -> int:
    """Whole days since this letter was released; 0 while it is a draft.

    Rendered next to the silent marker, because "seit 14 Tagen still" is
    actionable and "still" alone is a mood.
    """
    if row.released_at is None:
        return 0
    moment = now or dt.datetime.now(dt.UTC)
    return max((moment - row.released_at).days, 0)


# --- The relationship file --------------------------------------------------------

#: The states that mean the journalist came back. ``VEROEFFENTLICHT`` belongs in
#: here as well as in its own tally: the state machine keeps only the furthest
#: point a letter reached, so a reply followed by a piece leaves no ``antwort``
#: row behind, and counting the narrow way told the reader she never answered.
_ANSWERED: tuple[OutreachState, ...] = (
    OutreachState.ANTWORT,
    OutreachState.VEROEFFENTLICHT,
)

#: How many released letters one file renders. A ten-year relationship is a page
#: nobody scrolls, and the tallies are only trustworthy while the rows they were
#: counted from are on screen — so the cap is set well above an ordinary file and
#: the page says so when it is reached.
MAX_HISTORY = 200


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """One released letter in a contact's file, with the mandate it went out for.

    The mandate travels as its name rather than as the ``Client`` row because the
    name is all the file shows: the line says *for whom* the agency wrote, and
    both the chip label and its colour derive from the name alone.
    """

    letter: Outreach
    mandate: str


@dataclass(frozen=True, slots=True)
class Tally:
    """The four numbers over a contact's timeline: Anschreiben, Antworten,
    Veröffentlicht, ohne Reaktion."""

    anschreiben: int
    antworten: int
    veroeffentlicht: int
    still: int


class TimelineKind(StrEnum):
    """The two kinds of line a file holds, which are also the two sources.

    ``RELEASE`` is stamped by the machine the moment a person pressed the button;
    ``OUTCOME`` is a sentence somebody typed afterwards. The page has to tell them
    apart, so the distinction is a value rather than a template condition.
    """

    RELEASE = "release"
    OUTCOME = "outcome"


@dataclass(frozen=True, slots=True)
class TimelineEvent:
    """One line on a contact's timeline, with the moment it belongs to."""

    at: dt.datetime
    kind: TimelineKind
    letter: Outreach
    mandate: str


def timeline(history: list[HistoryEntry]) -> list[TimelineEvent]:
    """Every line the file renders, strictly newest first.

    A letter contributes up to two lines and they carry *different* timestamps:
    the release, and the outcome recorded later. So the outcome cannot simply be
    drawn above the letter it belongs to — a reply typed yesterday to a pitch from
    July belongs at the top of the page, above a letter released last week, and
    nesting the two would put July's reply below it. That interleaving is the
    common case, not an exotic one: a journalist answering an older pitch after a
    newer one has gone out is an ordinary week.

    Flattened here rather than in the template because "newest first" is a sort,
    and Jinja cannot do one over two timestamps on the same row.
    """
    events: list[TimelineEvent] = []
    for entry in history:
        letter = entry.letter
        events.append(
            TimelineEvent(
                # Never None: ``history_for_contact`` selects released rows only.
                at=letter.released_at,
                kind=TimelineKind.RELEASE,
                letter=letter,
                mandate=entry.mandate,
            )
        )
        if letter.outcome_at is not None:
            events.append(
                TimelineEvent(
                    at=letter.outcome_at,
                    kind=TimelineKind.OUTCOME,
                    letter=letter,
                    mandate=entry.mandate,
                )
            )
    # Ties are broken the way the query already breaks them (newest id first), and
    # an outcome sharing its letter's second sits above the release it answers.
    events.sort(
        key=lambda event: (
            event.at,
            event.letter.id,
            event.kind is TimelineKind.OUTCOME,
        ),
        reverse=True,
    )
    return events


def history_for_contact(
    session: Session, contact_id: int, *, limit: int = MAX_HISTORY
) -> list[HistoryEntry]:
    """The newest released letters to one contact, across all mandates.

    Across mandates on purpose (DEC-2): a journalist is a relationship the agency
    holds, and "have we already gone to her with something this month" cannot be
    answered from inside one client's workspace — so the mandate is named on
    every entry instead of scoping the query. Drafts never appear: the file is a
    record of what left the house, and a draft has not.

    Capped at ``limit`` (:data:`MAX_HISTORY`) newest first, so one long
    relationship cannot turn the page into a thousand-row render. The view says
    when the cap was reached; it never silently shows a shortened file.
    """
    rows = session.execute(
        select(Outreach, Client.name)
        .join(Client, Client.id == Outreach.client_id)
        .where(
            Outreach.contact_id == contact_id,
            Outreach.released_at.is_not(None),
        )
        .order_by(Outreach.released_at.desc(), Outreach.id.desc())
        .limit(limit)
    ).all()
    return [HistoryEntry(letter=letter, mandate=mandate) for letter, mandate in rows]


def tally(history: list[HistoryEntry], *, now: dt.datetime | None = None) -> Tally:
    """The tallies over a file, counted off the very rows the timeline shows, so
    the numbers and the entries beneath them can never disagree.

    The four numbers are **nested, not disjoint**: "Veröffentlicht" is a subset of
    "Antworten", because a piece that used the letter's thesis is an answer that
    went further, and the state machine only holds the furthest one a letter
    reached. Counted the narrow way, a journalist who replied and then published
    read "Antworten 0" — never answered — which is the opposite of what happened,
    and the locked mock's 4 / 3 / 2 / 1 could not occur at all.

    "Ohne Reaktion" is derived (:func:`is_silent`) rather than counted from a
    state, for the same reason it is not stored: silence is an absence. An
    ``absage`` sits on the timeline with its badge but has no tally of its own —
    a rejection is an answer of a kind, but claiming it as "Antwort" would count
    a door closing as a conversation.
    """
    letters = [entry.letter for entry in history]
    return Tally(
        anschreiben=len(letters),
        antworten=sum(row.state in _ANSWERED for row in letters),
        veroeffentlicht=sum(
            row.state == OutreachState.VEROEFFENTLICHT for row in letters
        ),
        still=sum(is_silent(row, now=now) for row in letters),
    )


def released_count_by_contact(session: Session) -> dict[int, int]:
    """Released letters per contact id — the roster's per-row count, fetched in
    one query for the whole page rather than one per contact. Drafts do not
    count: the number beside a name answers "how often did we actually go to
    them", not "how often did we consider it"."""
    rows = session.execute(
        select(Outreach.contact_id, func.count(Outreach.id))
        .where(
            Outreach.contact_id.is_not(None),
            Outreach.released_at.is_not(None),
        )
        .group_by(Outreach.contact_id)
    ).all()
    return dict(rows)


def for_angle(session: Session, angle_id: int) -> list[Outreach]:
    """Every message written off one impulse, newest first."""
    return list(
        session.scalars(
            select(Outreach)
            .where(Outreach.angle_id == angle_id)
            .order_by(Outreach.generated_at.desc(), Outreach.id.desc())
        ).all()
    )


def by_angle(session: Session, angle_ids: list[int]) -> dict[int, list[Outreach]]:
    """The messages for several impulses at once, keyed by angle id.

    One query for the page rather than one per card: the client view renders up to
    five impulses and the Today column renders one per mandate.
    """
    if not angle_ids:
        return {}
    grouped: dict[int, list[Outreach]] = {}
    for row in session.scalars(
        select(Outreach)
        .where(Outreach.angle_id.in_(angle_ids))
        .order_by(Outreach.generated_at.desc(), Outreach.id.desc())
    ).all():
        grouped.setdefault(row.angle_id, []).append(row)
    return grouped


__all__ = [
    "DEFAULT_RELEASED_BY",
    "MAX_HISTORY",
    "OUTCOMES",
    "STATE_LABELS",
    "HistoryEntry",
    "Tally",
    "TimelineEvent",
    "TimelineKind",
    "by_angle",
    "crosscheck",
    "days_out",
    "draft",
    "for_angle",
    "history_for_contact",
    "is_silent",
    "record_draft",
    "record_outcome",
    "record_sent",
    "release",
    "released_count_by_contact",
    "store",
    "tally",
    "timeline",
]
