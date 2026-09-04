"""The stakeholder card's shared web half: one note channel, one form reader.

The card is worked with from two pages — the register, where the standing map
stands, and the crisis page, where a selection is made in the hour it matters —
and its buttons live in three route modules (the map's writes with the mandate's
file, the selections with the occasion they hang on). What all of them share is
kept here rather than imported across sibling route modules:

* **one note channel for the whole feature.** Whichever of its buttons was
  pressed, the answer appears in the same place on the page, and no route
  module has to reach into another's dict to render its own page.
* **one reader for the order form,** so both pages refuse the same input for
  the same reason.
* **one place a model call is spent from.** Three of the card's buttons shell
  out to a model, and all three now go through :func:`spend` — off the request
  thread and behind one lock, the posture every other model-call button in
  this application already takes.

The sentences are constants because every one of them is a key in the i18n
table: a sentence built with an f-string cannot be looked up, and would render
German on an English page.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Collection

from sqlalchemy.orm import Session

from ...db import get_session
from .. import spawn

_log = logging.getLogger(__name__)

# Why the last stakeholder-card click produced what it produced, per mandate.
# In memory and not a schema change, the same posture as the register's own
# note: it describes one click, and going stale on a restart is correct.
_notes: dict[int, str] = {}

# One stakeholder model call at a time, process-wide: a second click while one
# is running would spend another. The same reason ``profile._researching`` and
# ``crisis_view._writing`` exist, and the same shape, so a reader who knows one
# of them knows this.
_calling = threading.Lock()

# Whose call holds the lock. The lock is process-global — one call at a time —
# but the *page* is per mandate, and mandate B's button must not present
# mandate A's run as running. Only ever read together with ``_calling.locked()``;
# a stale value under a released lock means nothing.
_calling_for: int | None = None

#: No profile, no map — the sentence about what is missing, and the page
#: carries the link to where it is filled in.
NO_PROFILE = (
    "Ohne Profilangaben wird keine Karte erfunden. Erst das Profil füllen, "
    "dann trägt der Vorschlag."
)
NO_NEW_GROUPS = "Der Vorschlag hat keine neuen Gruppen ergeben."
ROW_NOT_SAVED = (
    "Die Zeile wurde nicht gespeichert: es fehlt die Gruppe, oder der Name "
    "steht schon auf der Karte."
)
PROPOSAL_FAILED = "Der Vorschlag ist fehlgeschlagen. Die Einzelheiten stehen im Log."
NO_SELECTION = (
    "Keine Auswahl entstanden: ohne Karte oder ohne begründbar betroffene "
    "Gruppe wird nichts gespeichert."
)
SELECTION_FAILED = "Die Auswahl ist fehlgeschlagen. Die Einzelheiten stehen im Log."
ORDER_INCOMPLETE = (
    "Die Reihenfolge wurde nicht gespeichert: das Formular war unvollständig."
)
ORDER_DUPLICATE = (
    "Die Reihenfolge wurde nicht gespeichert: zwei Zeilen tragen dieselbe Nummer."
)
ORDER_WRONG_ROWS = (
    "Die Reihenfolge wurde nicht gespeichert: sie nennt nicht genau die Zeilen "
    "der Auswahl."
)
#: A second click while a call is running. Said rather than swallowed: the
#: button looks unpressed otherwise, and the reader clicks it again.
ALREADY_RUNNING = (
    "Es läuft schon eine Anfrage für dieses Mandat. Ein zweiter Klick würde "
    "eine zweite kosten."
)
#: A top-up that found nothing to add — distinct from "no selection at all",
#: because a list that already stands is not an empty one.
NO_NEW_SELECTED = (
    "Die Auswahl wurde nicht ergänzt: keine weitere Gruppe der Karte ist "
    "begründbar betroffen."
)

#: Every sentence this feature can put on a page. The i18n suite walks it, so a
#: note added without its English pair fails there rather than on the evening a
#: reader has the page in English.
NOTES = (
    NO_PROFILE,
    NO_NEW_GROUPS,
    ROW_NOT_SAVED,
    PROPOSAL_FAILED,
    NO_SELECTION,
    SELECTION_FAILED,
    ORDER_INCOMPLETE,
    ORDER_DUPLICATE,
    ORDER_WRONG_ROWS,
    ALREADY_RUNNING,
    NO_NEW_SELECTED,
)


def note(client_id: int, sentence: str) -> None:
    """Record why the last card click produced what it produced."""
    _notes[client_id] = sentence


def pop_note(client_id: int, *, owned: Collection[str] | None = None) -> str:
    """Hand the page the one-click note, clearing it.

    Popped, not read: a note describes one click, and showing it once is its
    whole job — left in the dict it would outlive the morning it belongs to.

    ``owned`` names the sentences the calling block renders, and leaves anything
    else standing for the block that owns it. One channel across every
    model-backed button on these pages is what keeps two clicks from spending
    two calls, but the answer is read where the button was pressed: a decision
    paper's sentence rendered inside the Stakeholder-Karte answers a click
    nobody made there. A caller that passes nothing is the page's catch-all and
    takes whatever is left, so it runs *after* the blocks that own theirs.
    """
    stored = _notes.get(client_id, "")
    if not stored or (owned is not None and stored not in owned):
        return ""
    return _notes.pop(client_id, "")


def busy(client_id: int) -> bool:
    """Whether a card model call is running *for this mandate* right now.

    Per mandate and not merely "the lock is held": the pages show a spinner off
    this, and mandate B announcing mandate A's run would be a lie about work
    nobody on that page started.
    """
    return _calling.locked() and _calling_for == client_id


def _run(job: Callable[[Session], str], client_id: int, failed: str) -> None:
    """Spend the one call on a worker thread; always give the lock back.

    The worker opens its own session: the request's is closed the moment the
    redirect is written, and a model call outliving it by three minutes must
    not be holding it open. ``job`` returns the sentence for the page, or the
    empty string where the click simply worked.
    """
    global _calling_for
    try:
        with get_session() as session:
            sentence = job(session)
        if sentence:
            note(client_id, sentence)
        else:
            # A note describes one click. Left standing, the last failure would
            # be read as the answer to the click that has just succeeded.
            _notes.pop(client_id, None)
    except Exception:  # noqa: BLE001 — a worker thread must never die silently
        # A fixed sentence and the cause in the log: an interpolated exception
        # cannot stand in the i18n table, so it would render German on an
        # English page — and a ParseError's text is the model's malformed
        # answer, which is nothing a reader can act on.
        _log.exception("a stakeholder card call for client %s failed", client_id)
        note(client_id, failed)
    finally:
        _calling_for = None
        _calling.release()


def spend(
    job: Callable[[Session], str], *, client_id: int, name: str, failed: str
) -> None:
    """Run one model-backed card job, at most one at a time, off the request.

    Every button of this feature that shells out to a model comes through here.
    Two reasons, and the second is the one that bites: a model call in the
    request handler blocks a worker for up to ``config.ANALYZER_TIMEOUT``
    (three minutes by default) while the browser waits on a redirect, and a
    second click while one is running would simply spend a second call.

    A refused click is *said*, not swallowed — an unanswered button reads as a
    broken one, and the reader presses it again.
    """
    global _calling_for
    if not _calling.acquire(blocking=False):
        note(client_id, ALREADY_RUNNING)
        return
    _calling_for = client_id

    def _release() -> None:
        global _calling_for
        _calling_for = None
        _calling.release()

    spawn.start_or_release(
        _run, args=(job, client_id, failed), name=name, release=_release
    )


def ordered_ids(sid: list[str], pos: list[str]) -> tuple[list[int], str]:
    """The row ids in the person's order, or the sentence saying why not.

    *Both* fields arrive as text on purpose, and are coerced here. A cleared
    position field posts an empty string; a hidden id field a script or a
    hand-edit has emptied posts one too. Either way FastAPI's raw 422 JSON is
    not an answer a person who pressed "Reihenfolge speichern" can do anything
    with, and the two fields of one form must not fail in two different ways.

    Two rows carrying the same number are refused rather than tie-broken. A
    tie-break would be the tool guessing half the call order, which the page
    would then present as "Reihenfolge gesetzt von <name>" — the one claim this
    feature exists not to make.
    """
    if not sid or len(sid) != len(pos):
        return [], ORDER_INCOMPLETE
    try:
        numbers = [int(value) for value in pos]
        ids = [int(value) for value in sid]
    except ValueError:
        return [], ORDER_INCOMPLETE
    if len(set(numbers)) != len(numbers):
        return [], ORDER_DUPLICATE
    return [row_id for _number, row_id in sorted(zip(numbers, ids, strict=True))], ""
