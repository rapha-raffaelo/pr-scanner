"""What an assistant answers when somebody asks about this market.

Two things live here, and they are deliberately kept apart.

:func:`propose` builds the question set a mandate is measured on. It is a
proposal and it stores nothing, for the reason ``rivals.py`` gives about
competitors and this feature inherits word for word: a wrong question silently
changes a number the agency reports to a client, and the number reads exactly as
well when it is wrong. A person accepts, and only then is there a set.

The set is banded by distance from the brand (:class:`~newspulse.models.
VisibilityBand`) because that is the whole difference between measuring
something and measuring nothing. "Was macht Enpal" hands the assistant the answer
in the question; "welche Anbieter für Solaranlagen mit Speicher gibt es" is where
a purchase starts, and it is the one the mandate is either in or not. So outside
the brand band a question that names the client is refused at generation rather
than filtered at display: it cannot measure whether the client is found.

:func:`measure` is the other half and it is deliberately unglamorous. Each
accepted question goes to each configured provider *verbatim* — no framing, no
instructions, exactly what a buyer would type — and a second pass reads the
answer back: which companies are named, in what order, which sources the model
stated. The answer is kept word for word, so every figure on the page resolves
to something a person can open and read rather than trust.

Three properties of the reading are load-bearing:

* **Named is decided here, not by the model.** A company counts as named when
  its own name or one of its stored aliases appears in the answer text
  (:mod:`newspulse.matching`), and the position is the rank of its first
  appearance. The reading model supplies the companies this tool has no stored
  name for; it never gets to decide whether the mandate was in the answer.
* **Rivals are the intersection with the stored competitor set.** A firm nobody
  put in that set counts as market, not as a rival, so the comparison stays the
  one share of voice already runs on.
* **A failure is a failure.** A provider that errors — or hands back nothing —
  goes into ``providers_failed`` on the run and writes no row. "Nobody answered"
  and "the answer did not name us" are different facts, and the second is the one
  a client acts on. An answer that came back and could not be read is a third
  fact again, counted on the run as ``answers_unread``: nobody failed to answer,
  so no provider is flagged, and the calls it cost were still spent.

Nothing here optimises anything and nothing here sends anything: it measures two
named assistants on a stated date, and says so.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from string import Template
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import brain, company_names, config, gemini, profile
from .analyzer import (
    AnalyzerError,
    ParseError,
    invoke_claude_cli,
    invoke_with_fallback,
    strip_code_fence,
)
from .matching import terms_matcher
from .models import (
    Client,
    VisibilityAnswer,
    VisibilityBand,
    VisibilityQuestion,
    VisibilityRun,
)

_log = logging.getLogger(__name__)

_PANEL_RESOURCE = "prompts/visibility_panel.txt"
_READ_RESOURCE = "prompts/visibility_read.txt"

#: How many questions a mandate may be measured on. Twenty-four is a spend
#: ceiling before it is anything else: every question is one call to every
#: provider, every week, for every mandate. It is also the point past which the
#: page stops being readable — a consultant reads a standing and four movements,
#: not sixty rows — and a set that large is no longer a set somebody chose.
MAX_QUESTIONS = 24

#: The two assistants a measurement asks (DEC-2 option A). Claude over the
#: subscription and Gemini over the key that is already there: no new access, no
#: new money, and the page names both rather than saying "die KI".
PROVIDER_CLAUDE = "claude"
PROVIDER_GEMINI = "gemini"

#: The profile fields a question set is built from. Four of eighteen, and the
#: four a buyer's question can actually be derived from: what the company sells,
#: to whom, under which product names, and how it says it differs. A founding
#: year and a press contact tell a model nothing about what somebody types into
#: an assistant before buying.
_PROFILE_KEYS = ("geschaeftsfeld", "zielgruppe", "produkte", "positionierung")

#: How much of one profile field travels into the prompt. The panel prompt reads
#: four of them plus the standing instructions, and a mandate whose
#: "Positionierung" is three paragraphs would otherwise push the band rules out
#: of the model's attention.
_PROFILE_VALUE_MAX = 400

#: How many companies and sources one answer may yield. A single answer that
#: names more than this is a list article, not a recommendation, and the extra
#: entries are noise in a ranking. Sliced rather than refused: a long answer is
#: still a measurement.
_LISTED_MAX = 24

#: Prefixes stripped from a stated source before it is checked against the
#: answer. A model writes "https://www.pv-magazine.de/" in one line and
#: "pv-magazine" in the next, and dropping a real citation over the scheme would
#: understate what the assistants are leaning on.
_URL_PREFIXES = ("https://", "http://", "www.")

#: What makes a stated source a locator rather than a name. A probe that
#: carries one of these is a domain or a path and is checked as a substring,
#: because that is how it is written into a sentence. Everything else is a
#: publisher's name and is checked on word boundaries: "FAZ" must be found in
#: "die FAZ schreibt" and never inside a longer word, which a bare substring
#: test cannot tell apart.
_LOCATOR_MARKS = (".", "/")

#: Endings that take no genitive -s in German. Compared against the case-folded
#: term, which is also what turns "ß" into "ss", so a name ending in it is
#: caught here too.
_GENITIVE_ENDINGS = ("s", "x", "z")

#: How many ask-failures a provider gets inside one measurement before the rest
#: of the set stops being put to it. Two, and both halves of that are
#: deliberate: an exhausted subscription fails identically for every question,
#: so it must not be asked twenty-four times — and a single transient error (one
#: 503, one timeout on a long answer) must not cost that provider a whole week,
#: because the next measurement is seven days away.
_PROVIDER_STRIKES = 2

#: How many answers may come back unreadable before the measurement stops. The
#: reading model is a second call on every cell and it fails identically for all
#: of them when it is down — without a budget here a broken reader spends every
#: provider call in the set (up to twenty-four questions times two providers) and
#: stores nothing, on this sweep and on every one after it. Same size as
#: :data:`_PROVIDER_STRIKES` and for the same reason: one answer nobody could
#: parse is not an outage.
_READ_STRIKES = 2

#: How long a started measurement may still be running before another one may
#: begin beside it. The run row is committed before the first provider is asked,
#: so a second sweep sees the claim and stays out; this is only the ceiling on
#: how long a process that died mid-run may keep the next attempt out. Well past
#: what a full set can take at ``ANALYZER_TIMEOUT``, and a small fraction of the
#: window, so a crash costs one attempt rather than the mandate's week.
_IN_FLIGHT_MAX = dt.timedelta(hours=6)

#: The fewest characters a stated source's host may keep, once its domain ending
#: is dropped, before it is looked for as a publisher's name. "finanztip.de"
#: against an answer that writes "Finanztip" is the citation this rescues;
#: "test.de" against an answer containing the ordinary word "Test" is the one it
#: must not invent, and five characters is where those two part — the same floor
#: :func:`~newspulse.matching.radar_matcher` uses to keep short tokens out of an
#: alternation.
_SOURCE_NAME_MIN = 5

#: List markers stripped off the front of a question before it is stored. A
#: question pasted out of a bulleted list carries one and it is not part of what
#: a buyer types — and a text beginning with a dash is read by ``claude -p`` as a
#: flag rather than as the prompt, so that provider would fail on it every week.
_LIST_MARKERS = "-–—*•·"

#: How many compiled matchers are kept. One measurement reads up to forty-eight
#: answers against the mandate and every stored competitor, and recompiling the
#: same alternation for each of them is most of what the reading itself costs.
_MATCHER_CACHE = 512


class SetFull(ValueError):
    """The accepted set is at :data:`MAX_QUESTIONS` and cannot take another.

    Raised rather than silently ignored: the consultant clicked "übernehmen" and
    is entitled to be told why the question did not appear, instead of looking
    for it in a list it was never added to.
    """

    def __init__(self, client_name: str) -> None:
        super().__init__(
            f"{client_name} hat bereits {MAX_QUESTIONS} Fragen im Satz; eine "
            "weitere geht erst, wenn eine ausgemustert wird"
        )


@dataclass(frozen=True, slots=True)
class Proposal:
    """One proposed question. Nothing is created from it without a click."""

    text: str
    band: VisibilityBand


@dataclass(frozen=True, slots=True)
class Reading:
    """What one answer says, in the terms every figure on the page is built from.

    ``companies`` is ordered by first appearance in the answer, and ``position``
    is this mandate's rank among them. Keeping the order rather than only the
    rank is what lets the number be checked against the answer beside it.

    The one place the two part company is an answer that names more than
    :data:`_LISTED_MAX` firms: the rank is counted over all of them and the list
    keeps the first of them, so ``position`` can point past the end of
    ``companies``. That way round on purpose — a rank counted over a cut list
    would read better than the answer supports.
    """

    named: bool
    position: int | None
    companies: tuple[str, ...]
    rivals: tuple[str, ...]
    sources: tuple[str, ...]


# --- Parsing --------------------------------------------------------------------
#
# The two response shapes live here rather than in ``schemas.py`` because neither
# is spoken anywhere else: a panel is read once, by :func:`propose`, and turned
# into ``Proposal`` in the same breath.


class _PanelQuestion(BaseModel):
    model_config = ConfigDict(extra="ignore")

    frage: str = ""
    #: A raw string, not the enum. An unrecognised band has to survive parsing so
    #: it can be *dropped* with a log line naming it; validating it here would
    #: fail the whole panel over one bad row.
    band: str = ""


class _Panel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    fragen: list[_PanelQuestion] = Field(default_factory=list)


class _ReadResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    unternehmen: list[str] = Field(default_factory=list)
    quellen: list[str] = Field(default_factory=list)


def _template(resource: str) -> Template:
    text = resources.files("newspulse").joinpath(resource).read_text("utf-8")
    return Template(brain.compose(text))


#: Bound to :class:`pydantic.BaseModel` so :func:`_parse` hands back the model it
#: was given rather than the base class, and its caller can read the fields.
_Parsed = TypeVar("_Parsed", bound=BaseModel)


def _parse(raw: str, model: type[_Parsed], what: str) -> _Parsed:
    try:
        payload = json.loads(strip_code_fence(raw))
    except json.JSONDecodeError as exc:
        raise ParseError(f"{what} was not valid JSON: {exc}") from exc
    try:
        return model.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 — pydantic raises its own type
        raise ParseError(f"{what} did not match the schema: {exc}") from exc


# --- The question set -----------------------------------------------------------


def _with_genitive(terms: Iterable[str]) -> list[str]:
    """``terms``, each also in the genitive a German sentence writes it in.

    German puts a company into a sentence in the genitive as readily as in the
    nominative — "Was kostet Enpals Solaranlage?" — and the word-boundary
    lookarounds in :func:`~newspulse.matching.terms_matcher` end the match at the
    "s", so the form does not match the name at all. Both places that shows up
    are the two this feature exists for: the guard would pass a question that
    names the mandate into a band that must not name it, and the reading would
    store ``named=False`` for an answer that named it — the wrong number, in the
    direction a client acts on.

    An apostrophised genitive ("Enpal's") already matches, because an apostrophe
    is not a word character. A name that already ends in a sibilant takes no
    genitive -s in German ("Siemens' Angebot"), and one ending in punctuation or
    a symbol ("Enpal B.V.", "1Komma5°") is not written in the genitive at all, so
    neither is widened.
    """
    widened: list[str] = []
    for term in terms:
        widened.append(term)
        tail = term.strip()
        if not tail or not tail[-1].isalnum():
            continue
        if not tail.casefold().endswith(_GENITIVE_ENDINGS):
            widened.append(f"{tail}s")
    return widened


def _terms_for(company: Client) -> list[str]:
    """A company's name and stored aliases, each also without its legal form.

    The legal-form variant is what makes "Enpal" in an answer count for a mandate
    entered as "Enpal B.V." — see :mod:`newspulse.company_names`. No keywords: a
    theme is not a name, and a mandate whose keyword is "Solaranlagen" would
    otherwise be read as named in every answer about solar panels.
    """
    return _with_genitive(
        variant
        for raw in [
            getattr(company, "name", "") or "",
            *(getattr(company, "aliases", None) or []),
        ]
        for variant in company_names.variants((raw or "").strip())
    )


@lru_cache(maxsize=_MATCHER_CACHE)
def _matcher(terms: tuple[str, ...]) -> re.Pattern[str] | None:
    """:func:`~newspulse.matching.terms_matcher`, compiled once per term set.

    The same handful of alternations — the mandate's, each competitor's — is
    searched against every one of a run's forty-eight answers, and the compile is
    the expensive half. Keyed on the terms themselves rather than on the company,
    so a name edited between two runs cannot be answered out of the cache.
    """
    return terms_matcher(terms)


def _first_at(folded: str, terms: Sequence[str]) -> int | None:
    """Where ``terms`` first appears in an already case-folded text, or ``None``.

    Word-boundary matching, so "Zolar" is not found inside "Zolarion" and a
    mandate called "Bahn" is not named by every "Autobahn".
    """
    matcher = _matcher(tuple(terms))
    if matcher is None:
        return None
    found = matcher.search(folded)
    return found.start() if found else None


def _one_line(text: str) -> str:
    """``text`` as one line, without the list marker a paste brings with it.

    Whitespace is collapsed because two spellings of one question are one
    question, and a leading "-" or "•" is dropped because it is punctuation from
    the list the consultant copied out of rather than part of what a buyer types
    — and because ``claude -p`` reads a leading dash as a flag, which would make
    that question fail on that provider every single week.
    """
    return " ".join((text or "").split()).lstrip(_LIST_MARKERS + " ").strip()


def _profile_block(session: Session, client: Client) -> str:
    """The mandate's file, as much of it as a buyer's question can be built from."""
    facts = profile.stored(session, client.id)
    lines: list[str] = []
    for key in _PROFILE_KEYS:
        row = facts.get(key)
        value = " ".join((getattr(row, "value", "") or "").split())
        if value:
            label = profile.FIELDS_BY_KEY[key].label
            lines.append(f"{label}: {value[:_PROFILE_VALUE_MAX]}")
    return "\n".join(lines) or "—"


def _banded(raw: str) -> VisibilityBand | None:
    """One proposal's band, or ``None`` when it is not one of the four."""
    try:
        return VisibilityBand((raw or "").strip().casefold())
    except ValueError:
        return None


def _proposals(
    client: Client,
    items: Iterable[_PanelQuestion],
    taken: Iterable[str],
    *,
    room: int,
) -> list[Proposal]:
    """The proposals worth putting in front of a person, in the order proposed.

    Two things are dropped here rather than displayed with a warning, because a
    proposal on the page is a proposal somebody can accept with one click:

    * a band nobody recognises, which is filed under no default — a question in
      the wrong band changes the share it is counted in, and reads correctly
      while it does;
    * outside ``marke``, a question containing the client's name or a stored
      alias, which cannot measure whether the client is found because the answer
      is already in the question.

    ``room`` is what the set can still take, not the cap: a mandate holding
    twenty-three accepted questions is offered one, because the other twenty-three
    proposals could only be clicked into a :class:`SetFull`.
    """
    names = terms_matcher(_terms_for(client))
    seen = {value.casefold() for value in taken}
    kept: list[Proposal] = []
    for item in items:
        text = _one_line(item.frage)
        band = _banded(item.band)
        if not text:
            continue
        if band is None:
            _log.info(
                "dropping a visibility proposal for %r: band %r is not one of the "
                "four, and a default would file it under a share it does not belong to",
                client.name,
                item.band,
            )
            continue
        if band is not VisibilityBand.MARKE and names and names.search(text.casefold()):
            _log.info(
                "dropping a visibility proposal for %r: %r names the mandate "
                "outside the brand band",
                client.name,
                text,
            )
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        kept.append(Proposal(text=text, band=band))
    if len(kept) > room:
        _log.info(
            "the panel for %r proposed %d usable questions; offering the %d the "
            "set still has room for",
            client.name,
            len(kept),
            room,
        )
    return kept[:room]


def propose(
    session: Session,
    client: Client,
    *,
    invoke: Callable[..., str] = invoke_with_fallback,
) -> list[Proposal]:
    """Propose a question set for ``client``. Never stores anything.

    Built from the mandate's profile and its measured industry term, because a
    question a buyer would really type cannot be derived from a company name. A
    question the mandate already carries is left out rather than offered again: an
    accept for it would be a no-op, and an unclickable proposal is noise on a page
    that is otherwise all decisions.

    Returns an empty list when the feature is switched off, when the set is
    already full — no call is spent on proposals nobody could accept — and, via
    the prompt's own refusal standard, when the model does not know the market
    well enough, which is the normal case for a young mandate.
    """
    if not config.VISIBILITY_ENABLED:
        return []
    room = MAX_QUESTIONS - len(accepted(session, client))
    if room <= 0:
        _log.info(
            "%r already carries the full set of %d visibility questions; no "
            "panel is asked, because nothing proposed could be accepted",
            client.name,
            MAX_QUESTIONS,
        )
        return []
    prompt = _template(_PANEL_RESOURCE).substitute(
        client_name=client.name,
        industry=client.industry or "—",
        country=client.country or "DE",
        profile=_profile_block(session, client),
    )
    panel = _parse(
        invoke(prompt, timeout=config.ANALYZER_TIMEOUT), _Panel, "the question set"
    )
    taken = [row.text for row in _questions(session, client)]
    return _proposals(client, panel.fragen, taken, room=room)


def _questions(session: Session, client: Client) -> list[VisibilityQuestion]:
    """Every question ever accepted for this mandate, retired ones included."""
    return list(
        session.scalars(
            select(VisibilityQuestion)
            .where(VisibilityQuestion.client_id == client.id)
            .order_by(VisibilityQuestion.id)
        ).all()
    )


def accepted(session: Session, client: Client) -> list[VisibilityQuestion]:
    """The set this mandate is measured on. Empty until somebody accepts one.

    Capped at :data:`MAX_QUESTIONS` on read as well as on accept: the cap is a
    spend ceiling, and a row that arrived by hand or from an older cap must not be
    able to spend past it.
    """
    rows = list(
        session.scalars(
            select(VisibilityQuestion)
            .where(
                VisibilityQuestion.client_id == client.id,
                VisibilityQuestion.accepted.is_(True),
            )
            .order_by(VisibilityQuestion.id)
        ).all()
    )
    if len(rows) > MAX_QUESTIONS:
        # Said out loud rather than inferred from a short list: the case this
        # cap guards against is a set that arrived past it, and the operator
        # looking for the six questions nobody is measuring has no other way to
        # find out that they are the ones being dropped.
        _log.info(
            "%r carries %d accepted visibility questions; the first %d are "
            "measured and the rest are not asked",
            client.name,
            len(rows),
            MAX_QUESTIONS,
        )
    return rows[:MAX_QUESTIONS]


def _stored(session: Session, client: Client, text: str) -> VisibilityQuestion | None:
    """The row this mandate already carries for this wording, whatever its case.

    Compared in Python rather than in SQL, because the UNIQUE this leans on is
    case-sensitive in SQLite: "Was kostet X?" accepted beside "was kostet X?"
    would be two rows, one question, and double the weight in every share it
    appears in. The set is capped, so this reads a few dozen rows at most.
    """
    key = text.casefold()
    for row in _questions(session, client):
        if row.text.casefold() == key:
            return row
    return None


def _band(band: VisibilityBand | str) -> VisibilityBand:
    """One of the four, however it was spelled. ``ValueError`` for anything else.

    Case-folded the way :func:`_banded` folds a model's answer: a band that comes
    back from a form field under the casing of its label ("Marke") is the same
    band, and refusing the accept over that spelling would fail a click nobody
    got wrong.
    """
    value = getattr(band, "value", band)
    return VisibilityBand(str(value or "").strip().casefold())


def _rebanded(
    session: Session, question: VisibilityQuestion, band: VisibilityBand
) -> VisibilityQuestion:
    """``question`` under ``band``, stored when that is a correction.

    The one thing a second accept of the same wording is not a no-op for. A
    misfiled band is exactly what :func:`_proposals` drops proposals to prevent —
    it changes the share the question is counted in and reads correctly while it
    does — so a consultant re-accepting a question under the right band has to be
    able to move it, rather than silently changing nothing.
    """
    if question.band is band:
        return question
    _log.info(
        "re-banding visibility question %s of client %s: %s -> %s",
        question.id,
        question.client_id,
        question.band.value,
        band.value,
    )
    question.band = band
    session.commit()
    return question


def accept(
    session: Session,
    client: Client,
    question: str,
    band: VisibilityBand | str,
    *,
    now: dt.datetime | None = None,
) -> VisibilityQuestion:
    """Put one proposed question into the set. The only thing that stores one.

    Idempotent in the wording and only in the wording: a second click on the same
    question — in any casing — returns the row the first one created rather than
    adding a duplicate, so a double submit cannot make one question count twice in
    a share, while a band that differs is taken as the correction it is. A retired
    question accepted again is taken back up rather than inserted beside itself,
    which is what keeps the answers it already produced attached to it.

    Raises :class:`SetFull` at the cap and ``ValueError`` for a band that is not
    one of the four — a question filed under a default is exactly what
    :func:`propose` drops proposals to prevent.
    """
    text = _one_line(question)
    if not text:
        raise ValueError("a visibility question needs a text")
    banded = _band(band)
    reference = _reference(now)
    standing = _stored(session, client, text)
    if standing is not None and standing.accepted:
        return _rebanded(session, standing, banded)
    if len(accepted(session, client)) >= MAX_QUESTIONS:
        raise SetFull(client.name)
    if standing is None:
        standing = VisibilityQuestion(
            client_id=client.id, text=text, band=banded, created_at=reference
        )
        session.add(standing)
    standing.band = banded
    standing.accepted = True
    standing.accepted_at = reference
    session.commit()
    return standing


def retire(session: Session, question: VisibilityQuestion) -> VisibilityQuestion:
    """Take one question out of the set without deleting what it measured.

    The flag rather than a delete, because :class:`~newspulse.models.
    VisibilityAnswer` points at this row: removing it would take every measurement
    it was ever part of with it, and the movement panel compares against a week
    whose questions still have to resolve.
    """
    question.accepted = False
    session.commit()
    return question


# --- Reading one answer ---------------------------------------------------------


def _ordered(placed: list[tuple[int, str]]) -> tuple[str, ...]:
    """Company names ordered by where they first appear, each named once.

    Deduped on the offset as well as on the name, because the two spellings of
    one company do not have to look alike: a stored "1Komma5°" and the "1Komma5"
    a model writes start at the same character of the answer, and they are one
    mention of one firm. Counting them twice would put a company in the list that
    is not in the market and push the mandate — and everything under it — one
    rank down, which is the figure the agency reports.

    The stored spelling wins a tie: it is appended first and this sort is stable.
    """
    seen: set[str] = set()
    at: set[int] = set()
    ordered: list[str] = []
    for offset, name in sorted(placed, key=lambda entry: entry[0]):
        key = name.casefold()
        if key in seen or offset in at:
            continue
        seen.add(key)
        at.add(offset)
        ordered.append(name)
    return tuple(ordered)


def _rank_of(companies: tuple[str, ...], name: str) -> int | None:
    """``name``'s 1-based rank in ``companies``, compared as :func:`_ordered` did.

    Case-insensitively, and that is the whole reason this is not ``list.index``.
    :func:`_ordered` dedupes on the case-folded name and keeps whichever spelling
    appeared first, so a mandate stored as "Enpal" beside a competitor row
    someone entered as "ENPAL" is in the list under the other spelling — and
    ``.index`` would raise ``ValueError`` mid-measurement, past the reach of the
    ``AnalyzerError`` handler in :func:`measure`, taking every row already
    collected in that run with it.
    """
    key = name.casefold()
    for index, entry in enumerate(companies):
        if entry.casefold() == key:
            return index + 1
    return None


@lru_cache(maxsize=_MATCHER_CACHE)
def _locator_matcher(probe: str) -> re.Pattern[str]:
    """Match a domain or path where the answer really writes it.

    Anchored left on something that is neither a word character nor a hyphen, so
    "test.de" is not found inside "warentest.de" or "stiftung-warentest.de" —
    a different publisher, and a citation the answer never made is the one thing
    the source list may not record. A dot to the left is allowed, because
    "www.test.de" and "hilfe.test.de" are that publisher. Anchored right on a
    word character so "handelsblatt.com" is not read out of ".company".
    """
    return re.compile(rf"(?<![\w-]){re.escape(probe)}(?!\w)")


def _publisher_name(probe: str) -> str | None:
    """The publisher inside a stated locator: its host, without its domain ending.

    The reading model normalises "Finanztip" to "finanztip.de" as readily as it
    copies a URL out of the answer, and an answer that names the publisher in
    words would otherwise lose the citation over a domain form it never wrote.
    The host only — a path is not a name — and only where enough of it survives
    to be one: "test.de" leaves "test", which half the German sentences about a
    comparison contain. Punctuation inside the host becomes a gap, so
    "stiftung-warentest.de" is found in "die Stiftung Warentest".
    """
    host = probe.split("/", 1)[0]
    name, _, ending = host.rpartition(".")
    if not name or not ending or len(name) < _SOURCE_NAME_MIN:
        return None
    return " ".join(part for part in re.split(r"[\W_]+", name) if part)


def _appears(folded_answer: str, source: str) -> bool:
    """Whether the answer really states this source, scheme and www aside.

    A locator — anything carrying a dot or a slash — is looked for where the
    answer writes it, and failing that under the publisher's name inside it. A
    publisher's name is matched on word boundaries: a short one ("FAZ", "taz")
    occurs inside ordinary words often enough that a substring test would record
    a citation the answer never made, which is the one thing the source list may
    not do.
    """
    probe = source.casefold().strip()
    for prefix in _URL_PREFIXES:
        probe = probe.removeprefix(prefix)
    probe = probe.rstrip("/")
    if not probe:
        return False
    if not any(mark in probe for mark in _LOCATOR_MARKS):
        return _named_in(folded_answer, probe)
    if _locator_matcher(probe).search(folded_answer) is not None:
        return True
    name = _publisher_name(probe)
    return name is not None and _named_in(folded_answer, name)


def _named_in(folded_answer: str, name: str) -> bool:
    """Whether the answer names ``name`` as a word rather than inside one."""
    matcher = _matcher((name,))
    return matcher is not None and matcher.search(folded_answer) is not None


def _sources(folded_answer: str, stated: Iterable[str]) -> tuple[str, ...]:
    """The sources the model stated, and only those.

    Nothing is derived from the answer text: no URL is scraped out of it, no
    publisher is inferred from a phrasing. A source that does not occur in the
    answer at all is dropped, because the source list is the one figure on the
    page whose whole job is to resolve to something a person can find in the text
    underneath it. A model citing nothing leaves an empty list, which is a
    finding.
    """
    kept: list[str] = []
    seen: set[str] = set()
    for raw in stated:
        source = " ".join((raw or "").split())
        key = source.casefold()
        if not source or key in seen:
            continue
        if not _appears(folded_answer, source):
            _log.debug("dropping source %r: the answer does not state it", source)
            continue
        seen.add(key)
        kept.append(source)
    if len(kept) > _LISTED_MAX:
        _log.debug(
            "an answer stated %d sources; keeping the first %d",
            len(kept),
            _LISTED_MAX,
        )
    return tuple(kept[:_LISTED_MAX])


def read_answer(
    client: Client, answer: str, listed: Sequence[str], stated: Sequence[str]
) -> Reading:
    """Turn one verbatim answer into the figures the page is built from.

    ``listed`` and ``stated`` are what the reading model extracted. They are
    treated as two different kinds of claim. A company the tool has a stored name
    for — the mandate, its competitors — is located in the answer text here, so
    the model never gets to decide whether the mandate was named; ``listed`` only
    contributes the companies nobody stored, and a name the answer does not
    actually contain is dropped rather than counted.
    """
    folded = answer.casefold()
    known: list[Client] = [client, *client.competitors]

    placed: list[tuple[int, str]] = []
    at = _first_at(folded, _terms_for(client))
    if at is not None:
        placed.append((at, client.name))
    rivals: list[str] = []
    for rival in client.competitors:
        found = _first_at(folded, _terms_for(rival))
        if found is None:
            continue
        placed.append((found, rival.name))
        rivals.append(rival.name)
    placed.extend(_placed_unknown(folded, listed, known))

    ordered = _ordered(placed)
    if len(ordered) > _LISTED_MAX:
        _log.debug(
            "an answer named %d companies; the rank is counted over all of them "
            "and the first %d are stored",
            len(ordered),
            _LISTED_MAX,
        )
    named = at is not None
    stored_rivals = set(rivals)
    return Reading(
        named=named,
        # Counted over every company the answer names, before the list is cut:
        # a rank against a truncated field is wrong in the direction that
        # flatters the mandate, which is the direction a client acts on.
        position=_rank_of(ordered, client.name) if named else None,
        companies=ordered[:_LISTED_MAX],
        # In the order they were named, not in the order they are stored: the
        # panel beside the answer reads down the answer.
        rivals=tuple(name for name in ordered if name in stored_rivals),
        sources=_sources(folded, stated),
    )


def _placed_unknown(
    folded: str, listed: Sequence[str], known: Sequence[Client]
) -> list[tuple[int, str]]:
    """Where the companies this tool stores no name for appear in the answer.

    Every one of them, not a first :data:`_LISTED_MAX` of them: the mandate's
    position is a rank among all the companies the answer names, so the counting
    happens over the whole list and only the stored list is cut.

    A company that is already placed under a stored name is skipped, and that is
    checked in both directions. A stored name ending in a symbol — "1Komma5°",
    "E.ON" — is not found inside the shorter spelling a model writes, so testing
    only one way lets the same firm in twice under two names.
    """
    placed: list[tuple[int, str]] = []
    for raw in listed:
        name = _one_line(raw)
        if not name or _is_stored(name, known):
            continue
        found = _first_at(folded, _with_genitive(company_names.variants(name)))
        if found is None:
            _log.debug("dropping company %r: the answer does not name it", name)
            continue
        placed.append((found, name))
    return placed


def _is_stored(name: str, known: Sequence[Client]) -> bool:
    """Whether ``name`` is one of these companies under either side's spelling."""
    folded = name.casefold()
    probes = company_names.variants(name)
    for one in known:
        terms = _terms_for(one)
        if _first_at(folded, terms) is not None:
            return True
        if any(_first_at(term.casefold(), probes) is not None for term in terms):
            return True
    return False


def _read(
    client: Client,
    question: str,
    answer: str,
    *,
    template: Template,
    invoke: Callable[..., str],
) -> Reading:
    """Put one answer through the reading prompt and back into figures.

    The template is passed in rather than built here: one measurement reads up to
    forty-eight answers, and composing the prompt each time would re-read the file
    and the stored standards for every one of them.
    """
    prompt = template.substitute(question=question, answer=answer)
    result = _parse(
        invoke(prompt, timeout=config.ANALYZER_TIMEOUT), _ReadResult, "the answer reading"
    )
    return read_answer(client, answer, result.unternehmen, result.quellen)


# --- The measurement ------------------------------------------------------------


def _ask_claude(question: str) -> str:
    return invoke_claude_cli(question, timeout=config.ANALYZER_TIMEOUT)


def _ask_gemini(question: str) -> str:
    return gemini.generate(question, timeout=config.ANALYZER_TIMEOUT)


def askers() -> dict[str, Callable[[str], str]]:
    """The providers a measurement can actually reach, in the order it asks them.

    Claude always, over the subscription the analyzer already runs on. Gemini only
    where a key is configured: a provider the deployment never set up would
    otherwise be recorded as failing every week, which reads as an outage rather
    than as a choice nobody made.

    A mapping and not an enum on purpose. DEC-2 asks the two that are already
    connected and says a third should later be a definition rather than a
    migration, which is also why ``VisibilityAnswer.provider`` is a plain string.
    """
    reachable: dict[str, Callable[[str], str]] = {PROVIDER_CLAUDE: _ask_claude}
    if config.gemini_configured():
        reachable[PROVIDER_GEMINI] = _ask_gemini
    return reachable


def _reference(now: dt.datetime | None) -> dt.datetime:
    """The moment a window is measured from: ``now``, or the clock.

    A naive value is read as UTC rather than rejected, the same reading
    :class:`~newspulse.models.UTCDateTime` gives one on the way into the
    database. Without it a naive ``now`` would raise ``TypeError`` on the
    subtraction against ``ran_at``, which always comes back aware.
    """
    reference = now or dt.datetime.now(dt.UTC)
    if reference.tzinfo is None:
        return reference.replace(tzinfo=dt.UTC)
    return reference


def latest_run(session: Session, client: Client) -> VisibilityRun | None:
    """This mandate's most recent measurement, or ``None`` if it has never run."""
    return session.scalar(
        select(VisibilityRun)
        .where(VisibilityRun.client_id == client.id)
        .order_by(VisibilityRun.ran_at.desc(), VisibilityRun.id.desc())
        .limit(1)
    )


def _standing(session: Session, client: Client) -> VisibilityRun | None:
    """The most recent run the window is counted from: one that spent the set.

    Not simply the most recent run. A run whose providers all errored carries no
    answer row and cost two calls per provider (:data:`_PROVIDER_STRIKES`), and
    letting it hold the window would mean one broken morning costs the whole
    week: the next request would hand back that empty run instead of retrying,
    and :func:`due` would say no for seven days over a momentary 503.

    A run whose answers came back and could not be *read* is the opposite case
    and counts. Nothing is stored either way, but the providers were paid: it is
    a measurement that was spent, and letting it fail open would put the whole
    set on every sweep for as long as the reading model is down. That is what
    ``answers_unread`` is on the run for — the page can say the answers arrived
    unreadable, which is neither "nobody answered" nor "we were not named".

    :func:`latest_run` deliberately still returns the newest run whatever it
    holds — a barren run is exactly what tells the page that both providers were
    down, and it stays stored and readable.
    """
    return session.scalar(
        select(VisibilityRun)
        .where(
            VisibilityRun.client_id == client.id,
            or_(VisibilityRun.answers.any(), VisibilityRun.answers_unread > 0),
        )
        .order_by(VisibilityRun.ran_at.desc(), VisibilityRun.id.desc())
        .limit(1)
    )


def _in_flight(
    session: Session, client: Client, *, now: dt.datetime | None = None
) -> VisibilityRun | None:
    """A measurement of this mandate that is still running, if there is one.

    The run row is committed before the first provider is asked, so a second
    sweep — or a manual trigger beside the nightly one — finds the claim and
    stays out instead of putting the same set to the same providers twice. Not a
    lock: two callers reading this in the same instant can still both proceed.
    It narrows the overlap from the length of a whole run, which is minutes, to
    the gap between one SELECT and one INSERT, which is what a single-writer
    deployment can honestly offer without a lock table.

    A claim older than :data:`_IN_FLIGHT_MAX` belongs to a process that died
    mid-run and is ignored: a crash has to cost one attempt, not the week.
    """
    running = session.scalar(
        select(VisibilityRun)
        .where(
            VisibilityRun.client_id == client.id,
            VisibilityRun.finished_at.is_(None),
        )
        .order_by(VisibilityRun.ran_at.desc(), VisibilityRun.id.desc())
        .limit(1)
    )
    if running is None:
        return None
    if _reference(now) - running.ran_at >= _IN_FLIGHT_MAX:
        _log.warning(
            "visibility run %s for %r never finished; measuring again rather "
            "than waiting on a process that is gone",
            running.id,
            client.name,
        )
        return None
    return running


def _is_due(standing: VisibilityRun, *, now: dt.datetime | None = None) -> bool:
    every = config.VISIBILITY_EVERY_DAYS
    if every <= 0:
        return True
    return _reference(now) - standing.ran_at >= dt.timedelta(days=every)


def due(session: Session, client: Client, *, now: dt.datetime | None = None) -> bool:
    """Whether this mandate may be measured again yet.

    False for a mandate with no accepted question, because there is nothing to
    measure and a due mandate the sweep cannot serve would be picked up every
    morning forever.
    """
    if not config.VISIBILITY_ENABLED or not accepted(session, client):
        return False
    standing = _standing(session, client)
    return standing is None or _is_due(standing, now=now)


def _row(
    question: VisibilityQuestion, provider: str, answer: str, reading: Reading
) -> VisibilityAnswer:
    return VisibilityAnswer(
        question_id=question.id,
        provider=provider,
        # Verbatim, and never through ``prose.plain``: that rule governs text
        # this tool writes, and editing a measurement is falsifying it.
        answer=answer,
        named=reading.named,
        position=reading.position,
        companies=list(reading.companies),
        rivals=list(reading.rivals),
        sources=list(reading.sources),
    )


@dataclass(slots=True)
class _Spend:
    """What one measurement has spent so far, and what stops it spending more.

    Both counters are stopping conditions rather than statistics. A provider that
    keeps erroring stops being asked, because an exhausted subscription fails
    identically twenty-four times. A reader that keeps failing stops the whole
    run, because it fails identically for every cell too — and its cells cost a
    provider call each before the reading is even attempted, so a broken reader
    with no budget spends the entire set to store nothing at all.
    """

    strikes: dict[str, int] = field(default_factory=dict)
    failed: set[str] = field(default_factory=set)
    unread: int = 0

    def strike(self, provider: str, client_name: str, reason: str) -> None:
        self.strikes[provider] = self.strikes.get(provider, 0) + 1
        self.failed.add(provider)
        _log.warning(
            "%s could not answer for %r (%s); recorded as a failed provider, "
            "not as an answer that did not name the mandate (strike %d of %d)",
            provider,
            client_name,
            reason,
            self.strikes[provider],
            _PROVIDER_STRIKES,
        )

    def exhausted(self, provider: str) -> bool:
        return self.strikes.get(provider, 0) >= _PROVIDER_STRIKES

    def unreadable(self) -> bool:
        return self.unread >= _READ_STRIKES


def _answer_from(
    provider: str,
    put: Callable[[str], str],
    question: str,
    client_name: str,
    spend: _Spend,
) -> str | None:
    """One provider call, or ``None`` when it did not produce an answer.

    A blank answer is one of those. Nothing came back, and storing it would put
    ``named=False`` on the page — "the answer did not name us" — for an answer
    that does not exist, which is the one confusion this feature is built to
    prevent. Both shipped providers already raise on empty output; this is what
    keeps a third one from quietly writing a negative fact.
    """
    try:
        answer = put(question)
    except AnalyzerError as exc:
        spend.strike(provider, client_name, str(exc))
        return None
    if not (answer or "").strip():
        spend.strike(provider, client_name, "an empty answer")
        return None
    return answer


def _reading_from(
    client: Client,
    question: VisibilityQuestion,
    answer: str,
    provider: str,
    *,
    template: Template,
    invoke: Callable[..., str],
    spend: _Spend,
) -> Reading | None:
    """One answer read back, or ``None`` when the reading model could not.

    The provider is *not* recorded as failed here: it answered.
    ``providers_failed`` means "this one could not answer", and one unparseable
    answer out of twenty-four must not flag the twenty-three that were measured
    as an outage. The cell is counted on the run instead, both as the budget that
    stops a dead reader from spending the rest of the set and as the fact that
    tells the page these answers arrived and could not be read.
    """
    try:
        return _read(client, question.text, answer, template=template, invoke=invoke)
    except AnalyzerError as exc:
        spend.unread += 1
        _log.warning(
            "could not read the %s answer for %r to question %s "
            "(%d characters, discarded unstored): %s",
            provider,
            client.name,
            question.id,
            len(answer),
            exc,
        )
        return None


def _measure_set(
    client: Client,
    questions: Sequence[VisibilityQuestion],
    asking: dict[str, Callable[[str], str]],
    *,
    run: VisibilityRun,
    reader: Template,
    invoke: Callable[..., str],
) -> None:
    """Put every question to every provider still standing, storing what reads."""
    spend = _Spend()
    for question in questions:
        for provider, put in asking.items():
            if spend.exhausted(provider) or spend.unreadable():
                continue
            answer = _answer_from(provider, put, question.text, client.name, spend)
            if answer is None:
                continue
            reading = _reading_from(
                client,
                question,
                answer,
                provider,
                template=reader,
                invoke=invoke,
                spend=spend,
            )
            if reading is None:
                continue
            run.answers.append(_row(question, provider, answer, reading))
        if spend.unreadable():
            _log.warning(
                "stopping the visibility measurement for %r: %d answers came "
                "back and none of them could be read, so the rest of the set "
                "would cost a provider call each and store nothing",
                client.name,
                spend.unread,
            )
            break
    run.providers_failed = sorted(spend.failed)
    run.answers_unread = spend.unread


def measure(
    session: Session,
    client: Client,
    *,
    ask: dict[str, Callable[[str], str]] | None = None,
    invoke: Callable[..., str] = invoke_with_fallback,
    now: dt.datetime | None = None,
) -> VisibilityRun | None:
    """Put the accepted set to every configured provider and store what came back.

    Returns the stored run unchanged when one is still inside the window, without
    spending a call: at most one measurement per mandate per
    ``NEWSPULSE_VISIBILITY_EVERY_DAYS``. Returns ``None`` when there is nothing to
    do at all — the feature is off, or the mandate has no accepted question, and
    in neither case is a model asked.

    A provider that errors is recorded on the run and, after
    :data:`_PROVIDER_STRIKES` failures, put nothing further this time. The
    questions it did reach keep their rows, and the ones it did not have none —
    which, with ``providers_failed``, is how the page tells "nicht gemessen" from
    "nicht genannt". A run in which nobody answered at all is stored, so the page
    can say both providers were down, but it does not hold the window: see
    :func:`_standing`.

    The run row is written before the first provider is asked, so a second sweep
    starting beside this one finds the claim and stays out rather than spending
    the same set twice.

    Synchronous and unbatched, and sized for the nightly sweep rather than for a
    request: a full set is up to :data:`MAX_QUESTIONS` questions times two
    providers times two model calls, each bounded only by ``ANALYZER_TIMEOUT``.
    Whoever wires it to a button has to put it behind the same background path
    the sweep uses, not inside the response.
    """
    if not config.VISIBILITY_ENABLED:
        return None
    questions = accepted(session, client)
    if not questions:
        return None
    standing = _standing(session, client)
    if standing is not None and not _is_due(standing, now=now):
        return standing
    running = _in_flight(session, client, now=now)
    if running is not None:
        _log.info(
            "a visibility measurement for %r has been running since %s; not "
            "starting a second one beside it",
            client.name,
            running.ran_at.isoformat(),
        )
        return running
    asking = askers() if ask is None else dict(ask)
    if not asking:
        _log.warning("no visibility provider is configured; %r not measured", client.name)
        return None

    reader = _template(_READ_RESOURCE)
    reference = _reference(now)
    run = VisibilityRun(
        client_id=client.id,
        ran_at=reference,
        providers_asked=list(asking),
        providers_failed=[],
    )
    session.add(run)
    # Committed before anything is asked: this row is the claim the guard above
    # reads, and a claim nobody can see stops nothing.
    session.commit()
    _measure_set(client, questions, asking, run=run, reader=reader, invoke=invoke)
    run.finished_at = _reference(now)
    if not run.answers:
        _log.warning(
            "the visibility measurement for %r produced no answer at all "
            "(asked %s, failed %s, unreadable %d)",
            client.name,
            ", ".join(run.providers_asked) or "nobody",
            ", ".join(run.providers_failed) or "nobody",
            run.answers_unread,
        )
    session.commit()
    return run


__all__ = [
    "AnalyzerError",
    "MAX_QUESTIONS",
    "PROVIDER_CLAUDE",
    "PROVIDER_GEMINI",
    "ParseError",
    "Proposal",
    "Reading",
    "SetFull",
    "accept",
    "accepted",
    "askers",
    "due",
    "latest_run",
    "measure",
    "propose",
    "read_answer",
    "retire",
]
