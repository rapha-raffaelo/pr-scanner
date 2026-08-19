"""The daily read of the mailbox: replies, filed against the letter they answer.

Once a day, with the existing sweep, this asks Gmail for the conversations that
belong to released letters, takes every message in them that this mailbox did not
send, and stores it as a reply to that letter.

What it does *not* do is the load-bearing part:

* **It asks for threads and never for a mailbox.** DEC-6 option A: the only
  conversations RauteOS can name are the ones it started itself, so the query is
  a thread id — one Gmail call per released letter and no search over anything
  else. A mailbox full of client contracts, invoices and personal mail is not
  fetched, which is why it cannot be stored. The sentence "your mailbox is not
  read" stays literally true for everything that is not a pitch.
* **It interprets nothing.** A reply is stored as text. The only state it may set
  is ``antwort`` — a human wrote back — and Absage or Veröffentlicht stay the
  consultant's reading (see :func:`newspulse.outreach.record_reply`): "danke,
  nichts für uns" and "schicken Sie mehr" are the same event to a matcher and
  opposite events to a PR consultant.
* **It never fails the sweep.** Google being unreachable, or access having been
  revoked in the account, is reported at ERROR and returned in the report. The
  daily run has already stored the day's coverage by then, and losing that to a
  mail failure would be trading the thing that works for the thing that did not.

Idempotency is Google's message id and the UNIQUE column that holds it: running
the sync twice over the same mailbox stores nothing new and moves no timestamp.
Every network call goes through the injected ``fetch`` that
:mod:`newspulse.gmail_link` already threads through everything, so no test here
reaches Google.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import gmail_link, outreach
from .models import Outreach, OutreachReply

_log = logging.getLogger(__name__)

#: How many threads in a row may fail before the sync gives up for today. The two
#: failures worth stopping for — Google unreachable, access revoked — answer the
#: same way for *every* thread, and each attempt costs a full request timeout, so
#: walking two hundred letters would turn a failed read into an hour-long sweep.
#: Three rather than one, because a single thread can fail on its own: a
#: conversation deleted in the mailbox answers 404 while the rest are fine, and
#: one such letter must not stop the others from being read.
_MAX_CONSECUTIVE_FAILURES = 3

#: Gmail's label on a message this mailbox sent. The outgoing letter that started
#: the thread carries it, and so does anything else sent from the account —
#: neither is a reply to anything.
_SENT_LABEL = "SENT"
_DRAFT_LABEL = "DRAFT"


@dataclass(frozen=True, slots=True)
class SyncReport:
    """What one read of the mailbox did, for the log and for a caller to assert.

    ``connected`` is false when no mailbox is linked at all, which is a no-op
    rather than a failure: this tool is expected to work with nothing connected,
    and a report that called that an error would put a red line in a clean sweep.
    """

    connected: bool = False
    threads: int = 0
    replies: int = 0
    answered: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _letters_with_threads(session: Session) -> dict[str, Outreach]:
    """The released letters that have a conversation, keyed by thread id.

    Both conditions matter and neither implies the other. ``released_at`` is what
    makes the letter part of the ledger — a draft composed in Gmail and never sent
    is not a pitch anyone made — and a thread id is what makes it *askable*: with
    no outgoing message of ours in a conversation there is nothing to ask Gmail
    for, and DEC-6 forbids the alternative of searching by sender.

    Keyed by thread rather than returned as a list, because two rows can name the
    same conversation (a letter re-pushed after its row was replaced), and the
    same thread fetched twice is a wasted call whose second copy would be dropped
    by the message id anyway. The oldest row wins, which is the letter that
    actually opened the conversation.
    """
    rows = session.scalars(
        select(Outreach)
        .where(
            Outreach.released_at.is_not(None),
            Outreach.gmail_thread_id != "",
        )
        .order_by(Outreach.id)
    ).all()
    by_thread: dict[str, Outreach] = {}
    for row in rows:
        by_thread.setdefault(row.gmail_thread_id, row)
    return by_thread


def _is_ours(message: gmail_link.ThreadMessage, mailbox: str) -> bool:
    """Whether this mailbox itself wrote the message, by two independent tests.

    Gmail's ``SENT`` label is the authority and would be enough on its own; the
    address comparison is the belt to its braces, because the one thing that must
    never happen here is filing this tool's own outgoing letter as the
    journalist's answer to it. A ``DRAFT`` is ours for the same reason and has
    not been sent to anybody at all.

    The address is compared case-insensitively: the local part of an address is
    formally case-sensitive, and no mail provider on earth treats it that way.
    """
    labels = set(message.label_ids)
    if _SENT_LABEL in labels or _DRAFT_LABEL in labels:
        return True
    return bool(mailbox) and message.from_email.casefold() == mailbox.casefold()


def _store(
    session: Session,
    letter: Outreach,
    message: gmail_link.ThreadMessage,
    *,
    now: dt.datetime,
) -> tuple[bool, bool]:
    """File one message as the reply to ``letter``.

    Returns whether it was stored and whether it moved the letter's state — the
    first is false for a message already on file, the second for a letter whose
    outcome a person has already recorded.

    The stored check is on Google's message id, which is also UNIQUE in the
    schema, so a second sweep over the same mailbox neither stores a second row
    nor touches the first one's ``fetched_at`` — the timestamp that says when
    this tool took a copy of somebody else's mail.
    """
    already = session.scalar(
        select(OutreachReply.id).where(
            OutreachReply.gmail_message_id == message.message_id
        )
    )
    if already is not None:
        return False, False
    reply = OutreachReply(
        outreach_id=letter.id,
        gmail_message_id=message.message_id,
        from_name=message.from_name,
        from_email=message.from_email,
        # Gmail states the moment on every message it hands back; this machine's
        # clock stands in only for an answer that somehow carried none, and it is
        # the read's own moment rather than an invented receipt time.
        received_at=message.received_at or now,
        body=message.body,
        fetched_at=now,
    )
    session.add(reply)
    # One transaction for the reply and the state it may move — see record_reply.
    return True, outreach.record_reply(session, letter, at=reply.received_at)


def _read_thread(
    session: Session,
    letter: Outreach,
    mailbox: str,
    *,
    fetch: gmail_link.Fetch | None,
    now: dt.datetime,
) -> tuple[int, int]:
    """Every message in one conversation that this mailbox did not send.

    Returns how many were newly stored and how many moved a letter to answered.
    Raises :class:`gmail_link.GmailError` when Gmail could not be read — the
    caller decides whether that is one bad thread or a mailbox that is gone.
    """
    stored = answered = 0
    for message in gmail_link.thread(letter.gmail_thread_id, fetch=fetch):
        if not message.message_id or _is_ours(message, mailbox):
            continue
        filed, moved = _store(session, letter, message, now=now)
        stored += filed
        answered += moved
    return stored, answered


def sync(
    session: Session,
    *,
    fetch: gmail_link.Fetch | None = None,
    now: dt.datetime | None = None,
) -> SyncReport:
    """Read the replies to every released letter, and file them.

    A no-op with no mailbox connected, which is the ordinary state of an
    installation that never linked one: the report says so and the daily run is
    unaffected.

    A failure to read one conversation is logged at ERROR and the next
    conversation is tried, because a thread deleted in the mailbox is a fact
    about that thread. A run of them is a fact about the connection —
    unreachable, or revoked at Google — and after
    :data:`_MAX_CONSECUTIVE_FAILURES` the sync stops rather than spending a
    request timeout per letter on an answer that will not change today.

    Every stored row survives a failure: each reply is committed as it is filed,
    so an unreadable thread costs the replies in *that* conversation and nothing
    that was already there.
    """
    link = gmail_link.connected()
    if link is None or not link.is_connected:
        _log.info("mail sync skipped: no mailbox connected")
        return SyncReport()

    moment = now or dt.datetime.now(dt.UTC)
    letters = _letters_with_threads(session)
    stored = answered = 0
    errors: list[str] = []
    consecutive = 0
    for thread_id, letter in letters.items():
        try:
            filed, moved = _read_thread(
                session, letter, link.email, fetch=fetch, now=moment
            )
        except gmail_link.GmailError as unread:
            # Never swallowed and never fatal: the mailbox is the one part of this
            # sweep that depends on somebody else's service being up.
            _log.error("Gmail thread %s could not be read: %s", thread_id, unread)
            errors.append(f"{thread_id}: {unread}")
            # A write may have been left half-applied by the raising call.
            session.rollback()
            consecutive += 1
            if consecutive >= _MAX_CONSECUTIVE_FAILURES:
                _log.error(
                    "mail sync stopped after %d unreadable thread(s) in a row; "
                    "%d of %d conversation(s) were read",
                    consecutive,
                    len(errors),
                    len(letters),
                )
                break
            continue
        stored += filed
        answered += moved
        consecutive = 0

    report = SyncReport(
        connected=True,
        threads=len(letters),
        replies=stored,
        answered=answered,
        errors=errors,
    )
    _log.info(
        "mail sync: %d thread(s), %d new reply/replies, %d letter(s) now answered, "
        "%d error(s)",
        report.threads,
        report.replies,
        report.answered,
        len(report.errors),
    )
    return report


__all__ = ["SyncReport", "sync"]
