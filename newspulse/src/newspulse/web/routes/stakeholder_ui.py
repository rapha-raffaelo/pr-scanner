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

The sentences are constants because every one of them is a key in the i18n
table: a sentence built with an f-string cannot be looked up, and would render
German on an English page.
"""

from __future__ import annotations

# Why the last stakeholder-card click produced what it produced, per mandate.
# In memory and not a schema change, the same posture as the register's own
# note: it describes one click, and going stale on a restart is correct.
_notes: dict[int, str] = {}

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
)


def note(client_id: int, sentence: str) -> None:
    """Record why the last card click produced what it produced."""
    _notes[client_id] = sentence


def pop_note(client_id: int) -> str:
    """Hand the page the one-click note, clearing it.

    Popped, not read: a note describes one click, and showing it once is its
    whole job — left in the dict it would outlive the morning it belongs to.
    """
    return _notes.pop(client_id, "")


def ordered_ids(sid: list[int], pos: list[str]) -> tuple[list[int], str]:
    """The row ids in the person's order, or the sentence saying why not.

    The numbers arrive as text on purpose: a cleared position field posts an
    empty string, and FastAPI's 422 page is not an answer a person who pressed
    "Reihenfolge speichern" can do anything with.

    Two rows carrying the same number are refused rather than tie-broken. A
    tie-break would be the tool guessing half the call order, which the page
    would then present as "Reihenfolge gesetzt von <name>" — the one claim this
    feature exists not to make.
    """
    if not sid or len(sid) != len(pos):
        return [], ORDER_INCOMPLETE
    try:
        numbers = [int(value) for value in pos]
    except ValueError:
        return [], ORDER_INCOMPLETE
    if len(set(numbers)) != len(numbers):
        return [], ORDER_DUPLICATE
    return [row_id for _number, row_id in sorted(zip(numbers, sid, strict=True))], ""
