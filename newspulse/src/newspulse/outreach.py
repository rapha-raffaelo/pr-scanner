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
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from importlib import resources
from string import Template

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, gemini, guide, prose
from .analyzer import ParseError, invoke_with_fallback, strip_code_fence
from .models import Analysis, Angle, Article, Client, Outreach, visible_coverage
from .pitch import PitchTarget
from .schemas import MessageReview, PersonalMessage

_log = logging.getLogger(__name__)

_PROMPT_RESOURCE = "prompts/outreach.txt"

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
    one: two drafts at the same journalist are two attempts, not two pitches."""
    journalist = (target.journalist or "") if target else ""
    outlet = (target.outlet or "") if target else ""
    existing = session.scalars(
        select(Outreach).where(
            Outreach.angle_id == angle.id,
            Outreach.journalist == journalist,
            Outreach.outlet == outlet,
        )
    ).first()
    row = existing or Outreach(angle_id=angle.id, client_id=client.id)
    row.journalist = journalist
    row.outlet = outlet
    # House style, enforced rather than requested: the prompt asks for no dashes
    # and the model relapses by the third paragraph. See newspulse.prose.
    row.subject = prose.plain(message.subject)
    row.message = prose.plain(message.message)
    row.hook = message.hook.strip()
    # A stored review always belongs to the text beside it: re-writing for the
    # same recipient clears the old verdict rather than letting it stand over a
    # letter it never read.
    row.review = "\n".join(review.concerns) if review else ""
    row.reviewed_by = reviewed_by if review else ""
    row.review_ok = review.send if review else True
    if review and review.fix:
        row.review = f"{row.review}\nZuerst ändern: {review.fix}".strip()
    row.generated_at = dt.datetime.now(dt.UTC)
    session.add(row)
    session.commit()
    return row


#: Who a release is recorded against when the caller names nobody. There are no
#: user accounts in this tool, so this follows ``ClientFact.filled_by``: the
#: interesting fact is that a person was in the loop, not which one.
DEFAULT_RELEASED_BY = "mensch"


def release(
    session: Session,
    row: Outreach,
    *,
    by: str = DEFAULT_RELEASED_BY,
    when: dt.datetime | None = None,
) -> Outreach:
    """Record that a person put the agency's name on this letter.

    The one act in the pipeline a machine may not perform, so it is stored rather
    than inferred. Releasing an already-released letter leaves the first stamp
    alone: the record is of the moment it went out, and a second click is not a
    second sending.
    """
    if row.released_at is None:
        row.released_at = when or dt.datetime.now(dt.UTC)
        row.released_by = (by or DEFAULT_RELEASED_BY).strip() or DEFAULT_RELEASED_BY
        session.commit()
    return row


def released_letters(
    session: Session,
    client_id: int,
    *,
    until: dt.datetime | None = None,
    since: dt.datetime | None = None,
) -> list[Outreach]:
    """Released letters for one mandate, oldest release first.

    Drafts are absent by construction rather than by a filter a caller has to
    remember: this is the only way the report reads the ledger, and a report may
    only credit outreach that actually left the house.

    ``since``/``until`` bound the *release*, not the drafting: a letter matters to
    a reporting period because of when it went out.
    """
    query = select(Outreach).where(
        Outreach.client_id == client_id,
        Outreach.released_at.is_not(None),
    )
    if since is not None:
        query = query.where(Outreach.released_at >= since)
    if until is not None:
        query = query.where(Outreach.released_at < until)
    return list(
        session.scalars(query.order_by(Outreach.released_at, Outreach.id)).all()
    )


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
    "draft",
    "crosscheck",
    "store",
    "release",
    "released_letters",
    "for_angle",
    "by_angle",
]
