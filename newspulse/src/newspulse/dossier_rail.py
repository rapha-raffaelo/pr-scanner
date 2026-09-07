"""RAUTE Intelligence: the six tiles beside the dossier (GEL-02).

The mock draws a column of engine tiles next to the case — coloured symbol
field, coloured result line, a mark at the right edge — and the submitted design
had them confirm one another with green ticks. That is the one thing this rail
refuses. The tool has modules, not engines, and a tick with no run behind it is
the most expensive kind of trust there is: it reads exactly like a checked box
and costs nothing to produce.

So the tiles keep the template's look and carry one extra rule. Every one of
them stands on a stored row somewhere else in this tool, names that row's state,
and links to the page the row was written on. Nothing here is measured at render
time, nothing is estimated, and — like the dossier it sits beside — nothing is
written, no model is called and no request leaves the process.

Three properties this module exists to hold:

* :class:`Signal` has **three** members and not two. "Etwas ist gelaufen und
  blieb ohne Einwand", "etwas ist gelaufen und hat einen Einwand" and "es ist
  nichts gelaufen" are three different mornings, and the third one is never the
  first. It is the same distinction :class:`~newspulse.models.CheckState` makes
  over a single text, applied to the six areas the rail reads.
* A tile with nothing behind it says so in a sentence and links to where the row
  would come from. A zero that means "nie gemessen" and a zero that means
  "gemessen und nichts gefunden" must not read alike: the first is a gap in the
  tool's own housekeeping, the second is a finding about the mandate.
* Figures live in the dataclasses here, sentences live in the template. A test
  can then pin a number without parsing German, and
  :func:`newspulse.i18n.translate` can still reach every word on the page.

The five stored answers themselves come from :func:`newspulse.opportunity
.intelligence`, which GEL-01 already reads them with. This module splits them
across the template's six names, decides each tile's mark, and hangs the link on
it — it does not re-derive a figure that already has one definition, because two
definitions of "wie viele Texte sind geprüft" is two answers nobody reconciles.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.orm import Session

from . import opportunity as dossier
from . import visibility
from .models import Client, ReputationState, VisibilityBand


class Signal(StrEnum):
    """The mark at the right edge of a tile.

    Three values because two would lie. A rail that only knew "gut" and
    "schlecht" would have to file "dazu ist nie etwas gelaufen" under one of
    them, and whichever it picked would be wrong in a way nobody notices: filed
    as good it becomes a green tick for an absence, filed as bad it becomes an
    alarm about a mandate that has simply never been measured.
    """

    #: Something ran and drew no objection. The only value that is a green tick,
    #: and it is unreachable from every empty state in this module.
    OHNE_EINWAND = "ohne-einwand"
    #: Something ran and what came back needs a person.
    EINWAND = "einwand"
    #: Nothing ran. Not a verdict about the mandate — a gap in the file.
    NICHTS = "nichts"


class TileKey(StrEnum):
    """The six areas, in the order the template stacks them."""

    POSITIONING = "positioning"
    MEDIA = "media"
    ASSET = "asset"
    QUALITY = "quality"
    VISIBILITY = "visibility"
    INTELLIGENCE = "intelligence"


#: The template's own names for the six. Proper nouns and deliberately not run
#: through ``t()``: they name the areas the design names, and a rail whose tiles
#: are called something else in English is a second vocabulary for one product.
_TITLES: dict[TileKey, str] = {
    TileKey.POSITIONING: "Positioning",
    TileKey.MEDIA: "Media & Relationship",
    TileKey.ASSET: "Communication Asset",
    TileKey.QUALITY: "Quality & Verification",
    TileKey.VISIBILITY: "AI Visibility & GEO",
    TileKey.INTELLIGENCE: "Communication Intelligence",
}

#: Which symbol sits in the coloured field. Ids of the sprite ``base.html``
#: draws once, so the rail cannot drift from the tab strip pointing at the same
#: page: the Quality tile and the Guide tab wear the same shield.
_ICONS: dict[TileKey, str] = {
    TileKey.POSITIONING: "ico-profil",
    TileKey.MEDIA: "ico-markt",
    TileKey.ASSET: "ico-texte",
    TileKey.QUALITY: "ico-guide",
    TileKey.VISIBILITY: "ico-ki",
    TileKey.INTELLIGENCE: "ico-wettbewerb",
}

#: The word on each tile's link. German source strings, translated where they
#: are rendered — the label is chrome, the href below is not.
_LINK_WORDS: dict[TileKey, str] = {
    TileKey.POSITIONING: "Profil ansehen",
    TileKey.MEDIA: "Kontaktbuch",
    TileKey.ASSET: "Texte",
    TileKey.QUALITY: "Guide-Prüfung",
    TileKey.VISIBILITY: "Sichtbarkeit",
    TileKey.INTELLIGENCE: "Reputationsband",
}


def _hrefs(client_id: int) -> dict[TileKey, str]:
    """Where each tile's value was written, and where it would be written.

    The same href in both cases on purpose: a tile with nothing behind it has to
    point at the page the missing row comes from, or the reader is told what is
    missing and left to find it. Every one of these is a route this app serves,
    and the test walks all six.
    """
    return {
        TileKey.POSITIONING: f"/client/{client_id}/profil",
        TileKey.MEDIA: "/contacts",
        TileKey.ASSET: f"/client/{client_id}/advice",
        TileKey.QUALITY: f"/client/{client_id}/guide",
        TileKey.VISIBILITY: f"/client/{client_id}/ki",
        TileKey.INTELLIGENCE: "/today",
    }


# --- The figures behind each tile -------------------------------------------------
#
# Two of the six need something ``opportunity.Intelligence`` does not carry: the
# media list's best-placed name and the band the visibility set was measured in.
# They wrap the GEL-01 value rather than restating its fields, so there stays
# exactly one definition of "wie viele Bylines" and "wie viele Antworten".


@dataclass(frozen=True, slots=True)
class MediaFigures:
    """The reachable share of the media list, and the name at the top of it."""

    reach: dossier.MediaReach
    #: The best-fitting journalist (DEC-5's ordering), or ``None`` on an empty
    #: list. Carried whole, so the tile can name the origin of the
    #: recommendation in ``pitch``'s own words rather than inventing one.
    lead: dossier.Recipient | None


@dataclass(frozen=True, slots=True)
class VisibilityFigures:
    """The newest measurement, and the band its questions were asked in."""

    run: dossier.Visibility
    #: The band the mandate was named in, furthest from the brand first, or
    #: ``None`` when it was named nowhere. See :func:`_best_band`.
    band: VisibilityBand | None


#: What a tile hands the template. A union rather than ``object``, so a reader
#: can see from here which shapes the partial has to render.
Figures = (
    dossier.Positioning
    | MediaFigures
    | dossier.Quality
    | VisibilityFigures
    | dossier.Reputation
)


@dataclass(frozen=True, slots=True)
class Tile:
    """One tile: what it is called, what it stands on, and what mark it wears."""

    key: TileKey
    title: str
    #: Sprite id for the coloured symbol field.
    icon: str
    signal: Signal
    #: The page the value came from — and, when there is no value, the page it
    #: would come from.
    href: str
    #: German label for that link, translated where it is rendered.
    link: str
    figures: Figures

    @property
    def measured(self) -> bool:
        """Whether a stored row stands behind this tile at all.

        Exactly the complement of :attr:`Signal.NICHTS`, and stated as a property
        so the template asks the question it actually means. The invariant it
        rests on is the module's whole point: nothing that never ran may carry a
        mark that says it went well.
        """
        return self.signal is not Signal.NICHTS


# --- The six marks ----------------------------------------------------------------
#
# One small function per tile, because each of them answers "was ist hier
# gelaufen" differently and a single table of conditions would hide that. What
# they share is the shape: the ``NICHTS`` branch comes first and is about the
# file, the other two are about the mandate.


def _positioning_signal(figures: dossier.Positioning) -> Signal:
    """No line on file is nothing run; a line nobody has confirmed is an objection.

    The threshold for "alt" is :func:`newspulse.profile.checked`'s, not a second
    one here: the rail and the profile page must not disagree about whether the
    same stamp is stale.
    """
    if not figures.value:
        return Signal.NICHTS
    checked = figures.checked
    if checked.never or checked.as_age:
        return Signal.EINWAND
    return Signal.OHNE_EINWAND


def _media_signal(figures: MediaFigures) -> Signal:
    """A list of names nobody can be reached at is a finding, not an empty list."""
    if not figures.reach.named:
        return Signal.NICHTS
    if not figures.reach.with_contact:
        return Signal.EINWAND
    return Signal.OHNE_EINWAND


def _asset_signal(figures: dossier.Quality) -> Signal:
    """Did anything get written, and is the set finished?

    "Gelaufen" here is: texts hang on the occasion. The objection is a set that
    is not through yet — something is written and cannot be released — which is
    a different sentence from the Quality tile's, where the objection is a
    checker that said no.
    """
    if not figures.texts:
        return Signal.NICHTS
    if figures.checked < figures.texts:
        return Signal.EINWAND
    return Signal.OHNE_EINWAND


def _quality_signal(figures: dossier.Quality) -> Signal:
    """Three texts nobody has read is *nothing run*, and never a green tick.

    This is the tile the rule was written for. A share would put the two zeros
    on top of each other — "0 Einwände" out of three unchecked texts reads as
    clean and is the exact opposite — so the run is counted as the checks that
    actually happened, and no check at all is :attr:`Signal.NICHTS`.
    """
    if figures.checked + figures.objected == 0:
        return Signal.NICHTS
    if figures.objected:
        return Signal.EINWAND
    return Signal.OHNE_EINWAND


def _visibility_signal(figures: VisibilityFigures) -> Signal:
    """Never measured, measured and not named, measured and named — three states.

    The middle one is the reason the rail is built this way at all: a run that
    was paid for and named the mandate nowhere shows the same 0 as a mandate the
    sweep has never touched, and only one of them is something to act on.
    """
    run = figures.run
    if run.measured_at is None:
        return Signal.NICHTS
    if run.answers == 0 or run.named_in == 0:
        return Signal.EINWAND
    return Signal.OHNE_EINWAND


def _reputation_signal(figures: dossier.Reputation) -> Signal:
    """``ruhig`` is a reading. No reading is not ``ruhig``.

    Printing the reassuring word for an absent measurement is the worst sentence
    this rail could carry, so the absence gets its own mark and its own sentence.
    """
    if figures.state is None:
        return Signal.NICHTS
    if figures.state is ReputationState.RUHIG:
        return Signal.OHNE_EINWAND
    return Signal.EINWAND


# --- The two reads GEL-01's value does not already carry --------------------------

#: The bands in order of distance from the brand. A mention in a ``problem``
#: answer is the one worth reporting — the buyer had not named a category, let
#: alone a supplier — and a ``marke`` mention is table stakes, because the
#: question handed the assistant the answer. So the tile names the furthest band
#: the mandate reached rather than the first one it finds.
_BAND_ORDER: tuple[VisibilityBand, ...] = (
    VisibilityBand.PROBLEM,
    VisibilityBand.KATEGORIE,
    VisibilityBand.AUSWAHL,
    VisibilityBand.MARKE,
)


def _best_band(session: Session, client: Client) -> VisibilityBand | None:
    """The furthest-from-the-brand band the newest run named the mandate in.

    Read here rather than folded into :class:`newspulse.opportunity.Visibility`
    because that value is GEL-01's and its shape is pinned by GEL-01's tests.
    The cost is one further indexed select for the same run on a page that is
    already reading nine tables, and the alternative — a second counting of the
    answers in this module — is the duplication the module docstring refuses.
    """
    run = visibility.latest_run(session, client)
    if run is None:
        return None
    named = {
        answer.question.band for answer in run.answers if answer.named
    }
    for band in _BAND_ORDER:
        if band in named:
            return band
    return None


def _lead(people: list[dossier.Recipient]) -> dossier.Recipient | None:
    """The best-placed name of the media list.

    ``opportunity.recipients`` already sorts by DEC-5's fit, best first, so the
    top of that list is the answer and re-sorting it here would be a second
    ordering of the same rows.
    """
    return people[0] if people else None


# --- The rail ---------------------------------------------------------------------


def _tile(
    key: TileKey, figures: Figures, signal: Signal, hrefs: dict[TileKey, str]
) -> Tile:
    """One tile, with the four constants that belong to its key."""
    return Tile(
        key=key,
        title=_TITLES[key],
        icon=_ICONS[key],
        signal=signal,
        href=hrefs[key],
        link=_LINK_WORDS[key],
        figures=figures,
    )


def rail(
    session: Session,
    client: Client,
    people: list[dossier.Recipient],
    stored_texts: list[dossier.Text],
    *,
    now: dt.datetime | None = None,
) -> list[Tile]:
    """The six tiles beside the dossier. Reads only; no model, no network.

    The media list and the texts arrive from the page rather than being fetched
    again, the same way GEL-01's rail takes them: a figure in a tile and the same
    figure in the column beside it come off one read and cannot drift apart.
    """
    reference = now or dt.datetime.now(dt.UTC)
    intel = dossier.intelligence(session, client, people, stored_texts, now=reference)
    hrefs = _hrefs(client.id)
    media = MediaFigures(reach=intel.media, lead=_lead(people))
    seen = VisibilityFigures(run=intel.visibility, band=_best_band(session, client))
    return [
        _tile(
            TileKey.POSITIONING,
            intel.positioning,
            _positioning_signal(intel.positioning),
            hrefs,
        ),
        _tile(TileKey.MEDIA, media, _media_signal(media), hrefs),
        _tile(TileKey.ASSET, intel.quality, _asset_signal(intel.quality), hrefs),
        _tile(TileKey.QUALITY, intel.quality, _quality_signal(intel.quality), hrefs),
        _tile(TileKey.VISIBILITY, seen, _visibility_signal(seen), hrefs),
        _tile(
            TileKey.INTELLIGENCE,
            intel.reputation,
            _reputation_signal(intel.reputation),
            hrefs,
        ),
    ]


__all__ = [
    "Figures",
    "MediaFigures",
    "Signal",
    "Tile",
    "TileKey",
    "VisibilityFigures",
    "rail",
]
