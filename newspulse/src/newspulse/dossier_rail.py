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
* A stored row is not evidence forever. The two tiles that stand on a scheduled
  sweep are bounded by :data:`_STALE_AFTER`, because "die neueste Zeile" and
  "eine aktuelle Messung" stop being the same sentence the moment a sweep is
  switched off — and the tile that keeps its green tick for a measurement from
  2023 is exactly the cheap trust the rest of this module refuses.

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

from . import config
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
        # Filtered to this mandate, and not the bare page: ``today_view`` builds
        # the band over the roster it is showing, so an unfiltered /today answers
        # with the portfolio's standing — a different measurement from the one
        # this tile just printed, reached by clicking the tile that printed it.
        TileKey.INTELLIGENCE: f"/today?client={client_id}",
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
    #: How many days old the row behind this tile is, once it is old enough that
    #: the rail refuses to call it current (:data:`_STALE_AFTER`). ``None`` on a
    #: fresh tile and on the four whose value is not a dated sweep — so the
    #: template prints the age exactly where it changes what the tile means.
    stale_days: int | None = None

    @property
    def measured(self) -> bool:
        """Whether a stored row stands behind this tile at all.

        Exactly the complement of :attr:`Signal.NICHTS`, and stated as a property
        so the template asks the question it actually means. The invariant it
        rests on is the module's whole point: nothing that never ran may carry a
        mark that says it went well.
        """
        return self.signal is not Signal.NICHTS


# --- How old a stored answer may be ------------------------------------------------

#: How old the newest row of a scheduled sweep may be before this rail stops
#: calling it current. Two of the tiles — the visibility measurement and the
#: reputation reading — stand on runs that happen on a timer, and neither
#: ``visibility.latest_run`` nor ``reputation.history`` has a recency bound: the
#: newest row *is* the answer however old it is. Without this the tile of a
#: mandate whose sweep was switched off in 2023 wears a green tick today, which
#: is the same untrue sentence as a tick over a run that never happened.
#:
#: Thirty days because both sweeps are far quicker than that: the visibility set
#: runs every :data:`newspulse.config.VISIBILITY_EVERY_DAYS` days (seven by
#: default) and the reputation reading is written daily. Past a month the honest
#: statement is not "ruhig" but "das misst hier niemand mehr". One constant for
#: both, so the rail cannot call the same age stale on one tile and current on
#: the next; the Positioning tile keeps :data:`newspulse.profile.AGE_AFTER`
#: because it must not disagree with the profile page about the same stamp.
_STALE_AFTER = dt.timedelta(days=30)


def _stale_days(at: dt.date | dt.datetime | None, *, now: dt.datetime) -> int | None:
    """The age of a stored sweep row in days, once it is past :data:`_STALE_AFTER`.

    ``None`` twice over, for two different reasons the caller does not have to
    tell apart: there is no row at all (that is :attr:`Signal.NICHTS`'s business,
    decided from the figure itself), or the row is still inside the window and
    its age is not worth a sentence.

    Counted in calendar days in the reader's zone, the way
    :func:`newspulse.profile.checked` counts them, and a stamp from the future —
    a clock skew on a restored backup — counts as today rather than as a
    negative age. Takes both a date and a datetime because the two rows behind
    it are stored differently: a measurement carries an instant, a reputation
    reading carries the day it read.
    """
    if at is None:
        return None
    zone = config.local_zone()
    day = at.astimezone(zone).date() if isinstance(at, dt.datetime) else at
    days = max(0, (now.astimezone(zone).date() - day).days)
    return days if days >= _STALE_AFTER.days else None


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
    """A name without an address is the objection, and one address is not none.

    The tick asks for the whole list and not for a first hit. One contact out of
    seventeen was passing here, and the tile then printed "1 von 17" in green
    beside a check mark — a list sixteen names short of being usable, marked as
    though it were done. There is no fraction that would be honest either: what
    the reader has to act on is the gap, so the mark carries the gap and the two
    numbers under it say how wide it is.
    """
    if not figures.reach.named:
        return Signal.NICHTS
    if figures.reach.with_contact < figures.reach.named:
        return Signal.EINWAND
    return Signal.OHNE_EINWAND


def _asset_signal(figures: dossier.Quality) -> Signal:
    """Did anything get written, and did anybody object to it?

    "Gelaufen" here is: texts hang on the occasion. The mark's own word is the
    constraint on the rest — :attr:`Signal.EINWAND` renders as "Gelaufen, mit
    Einwand" in a tooltip and in an ``aria-label``, so it may only stand where a
    checker actually said no. An unfinished set used to reach it, which made
    this tile assert an objection nobody had raised, one line above the Quality
    tile printing "Mit Einwand: 0" about the same three texts.

    So "unfertig" belongs to the Quality tile, which is the one that reads the
    checks, and this tile answers only its own question. The two share
    :class:`newspulse.opportunity.Quality` and read different fields of it.
    """
    if not figures.texts:
        return Signal.NICHTS
    if figures.objected:
        return Signal.EINWAND
    return Signal.OHNE_EINWAND


def _quality_signal(figures: dossier.Quality) -> Signal:
    """Three texts nobody has read is *nothing run*, and never a green tick.

    This is the tile the rule was written for. A share would put the two zeros
    on top of each other — "0 Einwände" out of three unchecked texts reads as
    clean and is the exact opposite — so the run is counted as the checks that
    actually happened, and no check at all is :attr:`Signal.NICHTS`.

    The partial case is the common one and needs the same refusal: Lucas has the
    pitch checked and leaves the Statement and the Q&A, and one clean check out
    of three used to be enough for the tick. It stood directly above this tile's
    own result line, "Mit Einwand: 0 · ungeprüft: 2", painted green. So the tick
    asks for the whole set — a text nobody has read is not a text that passed.
    """
    if figures.checked + figures.objected == 0:
        return Signal.NICHTS
    if figures.objected or figures.unchecked:
        return Signal.EINWAND
    return Signal.OHNE_EINWAND


def _visibility_signal(figures: VisibilityFigures, *, stale: bool) -> Signal:
    """Never measured, measured and not named, measured and named — three states.

    The middle one is the reason the rail is built this way at all: a run that
    was paid for and named the mandate nowhere shows the same 0 as a mandate the
    sweep has never touched, and only one of them is something to act on.

    ``stale`` is the fourth reading of the same row and folds into the second:
    a measurement past :data:`_STALE_AFTER` did happen, so it is not
    :attr:`Signal.NICHTS`, but a sweep that has not run for a month is not a
    current answer about the mandate either — it is a thing to go and look at.
    """
    run = figures.run
    if run.measured_at is None:
        return Signal.NICHTS
    if stale or run.answers == 0 or run.named_in == 0:
        return Signal.EINWAND
    return Signal.OHNE_EINWAND


def _reputation_signal(figures: dossier.Reputation, *, stale: bool) -> Signal:
    """``ruhig`` is a reading. No reading is not ``ruhig``, and an old one is not either.

    Printing the reassuring word for an absent measurement is the worst sentence
    this rail could carry, so the absence gets its own mark and its own sentence
    — and a reading from two years ago is the same sentence wearing a date:
    ``reputation.history`` hands back the newest row whatever its day, so
    without ``stale`` the tick outlives the sweep that earned it.
    """
    if figures.state is None:
        return Signal.NICHTS
    if stale or figures.state is not ReputationState.RUHIG:
        return Signal.EINWAND
    return Signal.OHNE_EINWAND


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


def _best_band(
    session: Session, client: Client, *, measured: dt.datetime | None
) -> VisibilityBand | None:
    """The furthest-from-the-brand band the newest run named the mandate in.

    Read here rather than folded into :class:`newspulse.opportunity.Visibility`
    because that value is GEL-01's and its shape is pinned by GEL-01's tests.
    The alternative — a second counting of the answers in this module — is the
    duplication the module docstring refuses.

    ``measured`` is the ``measured_at`` of the value ``intelligence`` just
    returned, and it is passed in so a mandate that has never been measured —
    every mandate on its first morning — reaches no database at all here.
    Where there is a run, the row, its answers and (through the ``selectin`` on
    :attr:`newspulse.models.VisibilityAnswer.question`) their questions are
    already in this session's identity map from ``opportunity._visibility``, so
    what is left is one indexed lookup and no further loading.
    """
    if measured is None:
        return None
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
    key: TileKey,
    figures: Figures,
    signal: Signal,
    hrefs: dict[TileKey, str],
    *,
    stale_days: int | None = None,
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
        stale_days=stale_days,
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
    seen = VisibilityFigures(
        run=intel.visibility,
        band=_best_band(session, client, measured=intel.visibility.measured_at),
    )
    # The two ages the rail bounds, read once and handed to both the mark and
    # the sentence: a tile that marks itself stale and does not say how old it
    # is has told the reader there is a problem and hidden its size.
    seen_age = _stale_days(intel.visibility.measured_at, now=reference)
    read_age = _stale_days(intel.reputation.day, now=reference)
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
        _tile(
            TileKey.VISIBILITY,
            seen,
            _visibility_signal(seen, stale=seen_age is not None),
            hrefs,
            stale_days=seen_age,
        ),
        _tile(
            TileKey.INTELLIGENCE,
            intel.reputation,
            _reputation_signal(intel.reputation, stale=read_age is not None),
            hrefs,
            stale_days=read_age,
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
