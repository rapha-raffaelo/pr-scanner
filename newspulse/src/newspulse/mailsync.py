"""The daily read of the mailbox: replies, filed against the letter they answer.

Once a day, with the existing sweep, this asks Gmail for the conversations that
belong to released letters, takes every message in them that this mailbox did not
send, and stores it as a reply to that letter.

What it does *not* do is the load-bearing part:

* **It asks for threads and never for a mailbox.** DEC-6 option A: the only
  conversations RauteOS can name are the ones it started itself, so the query is
  a thread id — one Gmail call per recently released letter and no search over
  anything else. A mailbox full of client contracts, invoices and personal mail
  is not fetched, which is why it cannot be stored. The sentence "your mailbox is
  not read" stays literally true for everything that is not a pitch.
* **It interprets nothing.** A reply is stored as text. The only state it may set
  is ``antwort`` — a human wrote back — and Absage or Veröffentlicht stay the
  consultant's reading (see :func:`newspulse.outreach.record_reply`): "danke,
  nichts für uns" and "schicken Sie mehr" are the same event to a matcher and
  opposite events to a PR consultant.
* **Only the recipient's own message is an answer.** Gmail threads a delivery
  failure into the conversation it failed to deliver, and an out-of-office note
  into the one that triggered it. Both are messages this mailbox did not send,
  and filing either as the journalist's answer would put a letter that never
  arrived into the ledger as answered — a row nobody can tell from a real reply,
  in the one record DEC-1 exists to make auditable. So the sender is compared
  against the address the letter actually went to, and everything else is stored
  as text without moving any state.
* **It never fails the sweep.** Google being unreachable, or access having been
  revoked in the account, is reported at ERROR and returned in the report. The
  daily run has already stored the day's coverage by then, and losing that to a
  mail failure would be trading the thing that works for the thing that did not.

Idempotency is Google's message id and the UNIQUE column that holds it: running
the sync twice over the same mailbox stores nothing new and moves no timestamp.
It is the *stored row* that is the key, which has one known consequence worth
writing down for whoever builds the retention rule ``fetched_at`` exists for:
deleting a single reply by hand, while its letter stays inside the window, lets
the next morning fetch and store that message again. The ledger is unharmed —
:func:`newspulse.outreach.record_reply` will not move an outcome twice — but the
deletion does not stick, and making it stick needs a tombstone on the message id
rather than a change here. Nothing in this tool deletes a reply today; the one
path that exists takes the letter with it (``ON DELETE CASCADE``), and a letter
that is gone is never asked about again.

Every network call goes through the injected ``fetch`` that
:mod:`newspulse.gmail_link` already threads through everything, so no test here
reaches Google.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import gmail_link, outreach
from .models import Contact, Outreach, OutreachReply

_log = logging.getLogger(__name__)

#: How many threads in a row may fail before the sync gives up for today. The two
#: failures worth stopping for — Google unreachable, access revoked — answer the
#: same way for *every* thread, and each attempt costs a full request timeout, so
#: walking two hundred letters would turn a failed read into an hour-long sweep.
#:
#: Only those count. A conversation deleted in the mailbox answers 404 forever
#: (:class:`gmail_link.GmailMissing`), and counting *that* towards the give-up
#: meant three such threads starved every letter behind them — every day, for as
#: long as the window lasts. Three rather than one, because even a connection
#: failure is worth one retry on the next conversation before the morning is
#: written off.
_MAX_CONSECUTIVE_FAILURES = 3

#: The column widths of :attr:`OutreachReply.from_name` and ``from_email``,
#: enforced here where a stranger's ``From`` header is written rather than
#: trusted to the column: SQLite stores an over-long value without complaint —
#: which is the unbounded row the width exists to prevent — and every other
#: backend raises on the commit, out of reach of this module's error handling.
#: The same bound sits beside ``released_by`` in :mod:`newspulse.outreach`.
_FROM_NAME_MAX = 200
_FROM_EMAIL_MAX = 320

#: How much of one reply is kept. ``body`` is a ``Text`` column, and a journalist
#: answering a long conversation carries every earlier message quoted underneath
#: — a few megabytes of the same thread, stored whole and then rendered whole
#: into the contact's file and onto the card. Far above any answer a person
#: actually types, so what this cuts is the quoted tail and never the reply.
_BODY_MAX = 50_000

#: Said in the text rather than silently, because a shortened quote that does not
#: say it was shortened is the one way this could misrepresent somebody's words.
_BODY_TRUNCATED = "\n[gekürzt]"

#: How long after it went out a letter is still asked about. Without a bound the
#: sweep asks Gmail once per letter the agency has *ever* released: at fifteen
#: letters a week, a year in that is several hundred sequential requests added to
#: every morning, growing forever, for conversations that ended in March.
#:
#: Ninety days rather than thirty: a pitch answered eight weeks later is an
#: ordinary event in trade press, and this window is the one thing standing
#: between such an answer and never being read at all. Past it the conversation
#: is not asked about again — the letter stays in the ledger, its stored replies
#: stay on the file, and only the daily question stops. What the window skipped
#: is logged on every run, so the narrowing is never silent.
_REPLY_WINDOW = dt.timedelta(days=90)


@dataclass(frozen=True, slots=True)
class SyncReport:
    """What one read of the mailbox did, for the log and for a caller to assert.

    ``connected`` is false when no mailbox is linked at all, which is a no-op
    rather than a failure: this tool is expected to work with nothing connected,
    and a report that called that an error would put a red line in a clean sweep.
    """

    connected: bool = False
    #: Conversations actually asked about, which after a give-up is fewer than
    #: were selected. It said "selected" once, and a caller asserting on it was
    #: told four threads had been read on a morning that managed three.
    threads: int = 0
    #: Conversations that were selected and never reached, because the sync gave
    #: up first. The other half of the number above, so the two together always
    #: account for the day's work.
    left_unread: int = 0
    replies: int = 0
    answered: int = 0
    #: Released letters older than :data:`_REPLY_WINDOW`, which were not asked
    #: about. Reported rather than merely skipped: a sync that silently narrows
    #: what it reads is indistinguishable from a mailbox with nothing in it.
    aged_out: int = 0
    #: Letters that share a conversation with an earlier one. Google's message id
    #: is UNIQUE, so the message itself is filed against the letter that opened
    #: the thread and cannot be filed twice; every letter naming the thread still
    #: moves to ``antwort``. Counted because "the answer is on the other card" is
    #: a thing the reader has to be able to find out.
    shared_threads: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _letters_with_threads(
    session: Session, *, since: dt.datetime
) -> tuple[dict[str, list[Outreach]], int, int]:
    """The recently released letters that have a conversation, keyed by thread id.

    All three conditions matter and none implies another. ``released_at`` is what
    makes the letter part of the ledger — a draft composed in Gmail and never sent
    is not a pitch anyone made — a thread id is what makes it *askable*: with no
    outgoing message of ours in a conversation there is nothing to ask Gmail for,
    and DEC-6 forbids the alternative of searching by sender. ``since``
    (:data:`_REPLY_WINDOW`) is what keeps the daily cost from growing with every
    letter the agency has ever sent, since each row here is one HTTPS request
    every morning for as long as it qualifies.

    Keyed by thread, because two rows can name the same conversation (a letter
    re-pushed after its row was replaced) and the same thread fetched twice is a
    wasted call whose second copy would be dropped by the message id anyway. All
    of them are kept rather than only the first: they are all letters in that
    conversation, and dropping the newer one from the day's work meant it never
    reached ``antwort`` and nothing anywhere said so.

    Newest thread first, and oldest letter first *inside* one. The order is
    load-bearing on both axes: the sync can give up on a bad morning, so the
    letters most likely to still be alive are asked about before the old ones,
    and inside a conversation the opening letter is the row a message is filed
    against.

    Returns the threads to read, how many letters the window left out, and how
    many letters share a conversation with an earlier one — so the caller can say
    all three rather than quietly reading less than it looks like.
    """
    asked = (
        Outreach.released_at.is_not(None),
        Outreach.gmail_thread_id != "",
    )
    rows = session.scalars(
        select(Outreach)
        .where(*asked, Outreach.released_at >= since)
        .order_by(Outreach.id.desc())
    ).all()
    by_thread: dict[str, list[Outreach]] = {}
    for row in rows:
        # Prepended, because the query hands them back newest first and a
        # conversation is read oldest first. The lists hold one row apart from
        # the rare re-push, so the cost of the insert is not a cost.
        by_thread.setdefault(row.gmail_thread_id, []).insert(0, row)
    aged_out = session.scalar(
        select(func.count())
        .select_from(Outreach)
        .where(*asked, Outreach.released_at < since)
    )
    shared = sum(len(letters) - 1 for letters in by_thread.values())
    return by_thread, aged_out or 0, shared


def _is_ours(message: gmail_link.ThreadMessage, mailbox: str) -> bool:
    """Whether this mailbox itself wrote the message, by two independent tests.

    Gmail's own labels are the authority and would be enough on their own —
    :func:`gmail_link.is_own_message`, so the vocabulary is spelled in the one
    module that owns it. The address comparison is the belt to those braces,
    because the one thing that must never happen here is filing this tool's own
    outgoing letter as the journalist's answer to it.

    The address is compared case-insensitively: the local part of an address is
    formally case-sensitive, and no mail provider on earth treats it that way.
    """
    if gmail_link.is_own_message(message):
        return True
    return bool(mailbox) and message.from_email.casefold() == mailbox.casefold()


def _recipients(
    session: Session,
    letters: list[Outreach],
    messages: list[gmail_link.ThreadMessage],
    mailbox: str,
) -> frozenset[str]:
    """Every address this conversation's letters were actually addressed to.

    Two sources, and both are records rather than guesses. The contact book entry
    resolved at release is the consultant's own answer to "who is this person";
    the ``To`` header of the mailbox's outgoing message is Gmail's record of
    where the letter went, and it is the one that still works for a letter whose
    recipient was never in the book.

    A set rather than one address, because the two can legitimately differ — a
    contact whose address was corrected after the letter went out — and either is
    good evidence that the sender is the recipient rather than a mail server.
    Casefolded: the local part of an address is formally case-sensitive and no
    provider on earth treats it that way.

    Empty is a real answer and the honest one: with no address on file, nothing
    here can tell the journalist's reply from a delivery failure, and the caller
    stores without moving any state rather than guessing.
    """
    known: set[str] = set()
    for letter in letters:
        if letter.contact_id is None:
            continue
        contact = session.get(Contact, letter.contact_id)
        if contact is not None and contact.email:
            known.add(contact.email.casefold())
    known.update(
        message.to_email.casefold()
        for message in messages
        if message.to_email and _is_ours(message, mailbox)
    )
    return frozenset(known)


def _is_answer(
    message: gmail_link.ThreadMessage, letter: Outreach, recipients: frozenset[str]
) -> bool:
    """Whether this message answers ``letter``, rather than merely sharing its
    conversation.

    Two questions, and a message has to pass both. **Who wrote it**: Gmail
    threads a delivery failure into the conversation it failed to deliver and an
    out-of-office note into the one that triggered it, and neither is the
    journalist writing back — reading them as one would file a letter that never
    arrived as answered, signed by the machine, in the ledger DEC-1 exists to
    make auditable. **When**: a message that reached the mailbox before the
    letter went out cannot be a reply to it, whatever else it is. Both leave the
    message stored; only the letter's state is withheld.
    """
    if message.from_email.casefold() not in recipients:
        return False
    if message.received_at is None or letter.released_at is None:
        return True
    return message.received_at >= letter.released_at


def _bounded(body: str) -> str:
    """One reply's text, cut to :data:`_BODY_MAX` and saying so if it was cut."""
    if len(body) <= _BODY_MAX:
        return body
    return body[:_BODY_MAX] + _BODY_TRUNCATED


def _store(
    session: Session,
    letters: list[Outreach],
    message: gmail_link.ThreadMessage,
    recipients: frozenset[str],
    *,
    now: dt.datetime,
) -> tuple[bool, int]:
    """File one message as a reply to the letter that opened this conversation.

    Returns whether it was stored and how many letters it moved to answered — the
    first is false for a message already on file, the second is zero for anything
    the recipient did not write and for a letter whose outcome a person has
    already recorded.

    Filed against one letter and one only, because Google's message id is UNIQUE
    across the table: the same mail cannot be two rows. Every letter in the
    conversation still moves, since the journalist answered all of them at once.

    The stored check is on that message id, so a second sweep over the same
    mailbox neither stores a second row nor touches the first one's
    ``fetched_at`` — the timestamp that says when this tool took a copy of
    somebody else's mail.
    """
    already = session.scalar(
        select(OutreachReply.id).where(
            OutreachReply.gmail_message_id == message.message_id
        )
    )
    if already is not None:
        return False, 0
    reply = OutreachReply(
        outreach_id=letters[0].id,
        gmail_message_id=message.message_id,
        # Cut to the columns' own widths at the write, never at the read: this is
        # a stranger's header and nothing upstream of here bounds it.
        from_name=message.from_name[:_FROM_NAME_MAX],
        from_email=message.from_email[:_FROM_EMAIL_MAX],
        # Gmail states the moment on every message it hands back; this machine's
        # clock stands in only for an answer that somehow carried none, and it is
        # the read's own moment rather than an invented receipt time.
        received_at=message.received_at or now,
        body=_bounded(message.body),
        fetched_at=now,
    )
    session.add(reply)
    # One transaction for the reply and the state it may move — see record_reply,
    # which commits both together.
    moved = sum(
        outreach.record_reply(session, letter, at=reply.received_at)
        for letter in letters
        if _is_answer(message, letter, recipients)
    )
    # And committed here for the message no letter moved for: a bounce is still
    # stored, and without this it would sit in the session until some later write
    # happened to carry it along.
    session.commit()
    return True, moved


def _read_thread(
    session: Session,
    letters: list[Outreach],
    mailbox: str,
    *,
    fetch: gmail_link.Fetch | None,
    now: dt.datetime,
) -> tuple[int, int]:
    """Every message in one conversation that this mailbox did not send.

    Returns how many were newly stored and how many moved a letter to answered.
    Raises :class:`gmail_link.GmailMissing` when the conversation is gone and
    :class:`gmail_link.GmailError` when Gmail could not be read at all — the
    caller tells the two apart, because only the second says anything about the
    connection.
    """
    thread_id = letters[0].gmail_thread_id
    messages = gmail_link.thread(thread_id, fetch=fetch)
    recipients = _recipients(session, letters, messages, mailbox)
    if not recipients:
        _log.warning(
            "no recipient address is on file for Gmail thread %s; its messages "
            "are stored but move no letter to answered",
            thread_id,
        )
    stored = answered = 0
    for message in messages:
        if not message.message_id or _is_ours(message, mailbox):
            continue
        filed, moved = _store(session, letters, message, recipients, now=now)
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

    Bounded to letters released inside :data:`_REPLY_WINDOW`, because every
    qualifying letter is one request every morning for as long as it qualifies.
    What the window left out is counted, logged and reported rather than silently
    dropped.

    A failure to read one conversation is logged at ERROR and the next
    conversation is tried. Only a failure that says something about the
    *connection* — unreachable, or revoked at Google — counts towards giving up,
    because those answer the same way for every thread and each attempt costs a
    full request timeout. A conversation deleted in the mailbox answers 404
    forever and says nothing about the others, so it never counts: three such
    threads used to starve every letter behind them, on every run, for the whole
    window.

    Every stored row survives a failure: each reply is committed as it is filed,
    so an unreadable thread costs the replies in *that* conversation and nothing
    that was already there.
    """
    link = gmail_link.connected()
    if link is None or not link.is_connected:
        _log.info("mail sync skipped: no mailbox connected")
        return SyncReport()

    moment = now or dt.datetime.now(dt.UTC)
    by_thread, aged_out, shared = _letters_with_threads(
        session, since=moment - _REPLY_WINDOW
    )
    if aged_out:
        _log.info(
            "mail sync: %d letter(s) released more than %d days ago are past the "
            "window and were not asked about",
            aged_out,
            _REPLY_WINDOW.days,
        )
    if shared:
        _log.warning(
            "mail sync: %d letter(s) share a conversation with an earlier one; "
            "their answers are filed against the letter that opened it",
            shared,
        )
    stored = answered = 0
    errors: list[str] = []
    consecutive = 0
    attempted = 0
    for thread_id, letters in by_thread.items():
        attempted += 1
        try:
            filed, moved = _read_thread(
                session, letters, link.email, fetch=fetch, now=moment
            )
        except gmail_link.GmailMissing as gone:
            # A conversation deleted in the mailbox, and a fact about that one
            # conversation only. Reported, never counted: the letters behind it
            # still have answers waiting, every morning, for as long as this
            # thread stays deleted.
            _log.error("Gmail thread %s is gone: %s", thread_id, gone)
            errors.append(f"{thread_id}: {gone}")
            session.rollback()
            continue
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
                    "%d of %d conversation(s) were left unread",
                    consecutive,
                    len(by_thread) - attempted,
                    len(by_thread),
                )
                break
            continue
        stored += filed
        answered += moved
        consecutive = 0

    report = SyncReport(
        connected=True,
        threads=attempted,
        left_unread=len(by_thread) - attempted,
        replies=stored,
        answered=answered,
        aged_out=aged_out,
        shared_threads=shared,
        errors=errors,
    )
    _log.info(
        "mail sync: %d thread(s) read, %d left unread, %d new reply/replies, "
        "%d letter(s) now answered, %d error(s)",
        report.threads,
        report.left_unread,
        report.replies,
        report.answered,
        len(report.errors),
    )
    return report


__all__ = ["SyncReport", "sync"]
